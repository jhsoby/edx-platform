"""
Django admin command to send untranslated data to Meta Wiki.
"""
import asyncio
import aiohttp
import json
import pytz
from logging import getLogger
from datetime import datetime, timedelta
from collections import Counter


from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from opaque_keys.edx.keys import CourseKey, UsageKey
from django.utils import timezone

from lms.djangoapps.courseware.courses import get_course_by_id
from openedx.features.wikimedia_features.meta_translations.models import (
    WikiTranslation, CourseTranslation, CourseBlock, CourseBlockData, MetaTranslationConfiguration,
    MetaCronJobInfo,
)
from openedx.features.wikimedia_features.meta_translations.meta_client import WikiMetaClient
from openedx.features.wikimedia_features.meta_translations.utils import is_block_translated

log = getLogger(__name__)


class Command(BaseCommand):
    """
    This command will check and send updated block strings to meta server for translations.

        $ ./manage.py cms sync_translated_strings_to_edx_from_meta
        It will only show all blocks that are ready to fetched from meta.

        $ ./manage.py cms sync_translated_strings_to_edx_from_meta --commit
        It will send API calls of Wiki Meta to fetch updated translations from meta.
    """
    help = 'Command to sync/fetch updated translations from meta to edX'

    _UPDATED_TRANSLATIONS = []
    _UPDATED_BLOCKS = set([])

    def add_arguments(self, parser):
        """
        Add --commit argument with default value False
        """
        parser.add_argument(
            '--commit',
            action='store_true',
            help='Send API calls to Meta wiki',
        )

    def _log_final_report(self, request_data_dict):
        """
        Log final results.
        """
        log.info('\n\n\n')
        log.info("--------------------- WIKI META FETCHED PAGES RESULT- {} ---------------------".format(
            datetime.now().date().strftime("%m-%d-%Y")
        ))
        log.info("Request data dict: {}".format(json.dumps(request_data_dict, indent=4)))
        log.info("Updated Translations: {}".format(json.dumps(self._UPDATED_TRANSLATIONS, indent=4)))
        log.info("Updated Blocks: {}".format(self._UPDATED_BLOCKS))

    def is_translated(self, wiki_translation_obj):
        """
        Returns boolean value indicating if object is translated or not.
        """
        if wiki_translation_obj.translation:
            is_translated = True
            if wiki_translation_obj.source_block_data.parsed_keys:
                existing_translation = json.loads(wiki_translation_obj.translation)
                for key, value in wiki_translation_obj.source_block_data.parsed_keys.items():
                    if existing_translation.get(key) == None:
                        is_translated = False
                        break;
            return is_translated
        else:
            return False

    def _get_transation_objects_for_fetch_call(self):
        """
        Returns list of WikiTranslation objects for fetch call.
        Translations fot Blocks will only be checked at meta server in following cases:
        1. If blocks are not translated -> translations or last_fetched is None
        2. If blocks are translated -> we'll only check for latest updates/commits of meta server
           only if 3 days have been passed since last_fetched.
        3. If base course block content is changed don't include those translations. We need to first send
           updated content to Translatewiki
        """
        tranlsation_objects = []
        meta_config = MetaTranslationConfiguration.current()
        days_settings = settings.FETCH_CALL_DAYS_CONFIG_DEFAULT
        if meta_config and meta_config.enabled:
            days_settings = meta_config.days_settings_for_fetch_call

        comparison_date = (timezone.now() - timedelta(days=days_settings)).date()
        for obj in WikiTranslation.objects.all().select_related("target_block").select_related(
            "source_block_data", "source_block_data__course_block"
        ):
            if not obj.source_block_data.content_updated and (not obj.last_fetched or not self.is_translated(obj) or obj.last_fetched.date() <=comparison_date):
                tranlsation_objects.append(obj)
        return tranlsation_objects

    def _get_request_data_dict(self):
        """
        Returns dict of data required to fetch updated translations from Wiki Meta.
        Translations need to be fetched only if direction_flag of target block is destination.

        i.e for source block => block-v1:edX+fresh1+fresh1+type@chapter+block@a7e862b4a8b34c8f9c4870b44cbed97b
        {
            "block-v1:edX+fresh1+fresh1+type@chapter+block@a7e862b4a8b34c8f9c4870b44cbed97b": {
                "source_course_id": "source_course_id_1",
                "source_course_lang_code": "en",
                "target_block_versions" : {
                    "fr": "block-v1:edX+fresh1+duplicate_str+type@chapterblock@a7e862b4a8b34c8f9c4870b44cbed97b",
                    "hi": "block-v1:edX fresh1 hindi_ver2+type@chapter+block@a7e862b4a8b34c8f9c4870b44cbed97b",
                }
            }
            ...
        }
        """
        translation_objects = self._get_transation_objects_for_fetch_call()
        data_dict = {}
        for translation_obj in translation_objects:
            source_block = translation_obj.source_block_data.course_block
            target_block = translation_obj.target_block
            source_block_key = str(source_block.block_id)
            if not target_block.is_source():
                target_language = get_course_by_id(target_block.course_id).language
                source_language = get_course_by_id(source_block.course_id).language
                if source_block_key in data_dict:
                    data_dict[source_block_key]["target_block_versions"][target_language] = str(target_block.block_id)
                else:
                    data_dict[source_block_key] = {
                        "source_course_id": str(source_block.course_id),
                        "source_course_lang_code": source_language,
                        "target_block_versions": {
                            target_language: str(target_block.block_id),
                        }
                    }
        return data_dict

    def _update_result_list(self, target_block_id, source_block_data_type, target_lang, msg, status):
        """
        Update Final Result list i.e _UPDATED_TRANSLATIONS that will be used for final result logs
        """
        self._UPDATED_TRANSLATIONS.append({
            "target_block_id": target_block_id,
            "source_block_data_type": source_block_data_type,
            "target_language_code": target_lang,
            "message": msg,
            "translate_wiki_status": dict(Counter(status)) if isinstance(status, list) else status
        })

    def _update_translations_in_db(self, translation_obj, updated_translation, updated_commits, source_block_data, target_lang_code, status):
        translation_obj.translation = updated_translation
        translation_obj.approved = False
        translation_obj.approved_by = None
        translation_obj.last_fetched = datetime.now()
        translation_obj.fetched_commits = updated_commits
        translation_obj.save()

        translation_obj.target_block.applied = False
        translation_obj.target_block.save()

        log.info("Translations have been updated for block_id: {}, data_type: {}, target_language: {}".format(
            str(translation_obj.target_block.block_id),
            source_block_data.data_type,
            target_lang_code
        ))
        self._update_result_list(
            str(translation_obj.target_block.block_id), source_block_data.data_type, target_lang_code, "Updated", status
        )
        self._UPDATED_BLOCKS.add(str(translation_obj.target_block.block_id))

    def _check_and_update_translations(self, response_data, target_block_id, target_language_code):
        """
        Update WikiTranslations with fetched translated strings from meta-server

        Arguments:
            response_data (Dict): contains translations
            target_block_id (String): Blocks that need to be updated
            target_language_code (String): target block language.

        Sample response_data:
        "display_name": {
            "key": "Course-v1:edX+fresh1+fresh1/en/block-v1:edX+fresh1+fresh1+type@problem+block@9eefa6c9923346b1b746988401c638ad/display_name",
            "translation": "चेक बॉक्",
            "properties": {
                "revision" : 1232323,
                "reviewers": [4876]
                "status": "translated",
                "last-translator-text": "wikimeta-translator-username",
                "last-translator-id": "wikimeta-translator-userid",
            },
            "title": "Translations:[CoursePrefix]Course-v1:edX+fresh1+fresh1/en/block-v1:edX+fresh1+fresh1+type@problem+block@9eefa6c9923346b1b746988401c638ad/display_name/hi",
            "targetLanguage": "hi",
            "primaryGroup": "messagebundle-[CoursePrefix]Course-v1:edX+fresh1+fresh1/en/block-v1:edX+fresh1+fresh1+type@problem+block@9eefa6c9923346b1b746988401c638ad",
        },
        ...
        """
        translated_status = set(['translated', 'proofread'])
        wiki_translations = WikiTranslation.objects.filter(
            target_block__block_id=target_block_id
        ).select_related("target_block").select_related("source_block_data", "source_block_data__course_block")
        for translation_obj in wiki_translations:
            source_block_data = translation_obj.source_block_data
            if not translation_obj.translation or not translation_obj.last_fetched or not translation_obj.fetched_commits:
                # No need to compare commits, save translations and commits in DB directly
                fetched_commits = {}
                if source_block_data.parsed_keys:
                    translated_data = {}
                    key_status = []
                    for key, value in source_block_data.parsed_keys.items():
                        key_response = response_data.get(key, {})
                        if key_response.get('properties', {}).get('status') in translated_status:
                            translated_data.update({key: key_response.get('translation')})
                            fetched_commits.update({key: key_response.get('properties', {}).get('revision')})
                        key_status.append(key_response.get('properties', {}).get('status'))
                    if translated_data:
                        translated_data = json.dumps(translated_data)
                        self._update_translations_in_db(translation_obj, translated_data, fetched_commits, source_block_data, target_language_code, key_status)
                    else:
                        self._update_result_list(
                            str(translation_obj.target_block.block_id), source_block_data.data_type, target_language_code,
                            "Successfully fetched but no key is translated or reviewed", key_status, 
                        )
                else:
                    key_response = response_data.get(source_block_data.data_type, {})
                    key_status = key_response.get('properties', {}).get('status')
                    if key_status in translated_status:
                        translated_data = response_data.get(source_block_data.data_type, {}).get('translation')
                        fetched_commits.update({source_block_data.data_type: key_response.get('properties', {}).get('revision')})
                        self._update_translations_in_db(translation_obj, translated_data, fetched_commits, source_block_data, target_language_code, key_status)
                    else:
                        self._update_result_list(
                            str(translation_obj.target_block.block_id), source_block_data.data_type, target_language_code,
                            "Successfully fetched but status is not translated or reviewed", key_status,
                        )
            else:
                # Compare commits of tranlsations with existing db commits -> only update translations if commits are updated.
                existing_translation = translation_obj.translation
                existing_commits = translation_obj.fetched_commits
                if source_block_data.parsed_keys:
                    existing_translation = json.loads(existing_translation)
                    is_any_key_updated = False
                    key_status = []
                    for key, value in source_block_data.parsed_keys.items():
                        key_response = response_data.get(key, {})
                        key_commit = key_response.get('properties', {}).get('revision')
                        if key_response.get('properties', {}).get('status') in translated_status and not existing_commits.get(key) or (key_commit and key_commit != existing_commits.get(key)):
                            existing_translation.update({key: key_response.get('translation')})
                            existing_commits.update({key: key_commit})
                            is_any_key_updated = True
                        key_status.append(key_response.get('properties', {}).get('status'))
                    existing_translation = json.dumps(existing_translation)
                else:
                    is_any_key_updated = False
                    key_response = response_data.get(source_block_data.data_type, {})
                    key_commit = key_response.get('properties', {}).get('revision')
                    source_block_data.data_type
                    key_status = key_response.get('properties', {}).get('status')
                    if key_status in translated_status and not existing_commits.get(source_block_data.data_type) or (key_commit and key_commit != existing_commits.get(source_block_data.data_type)):
                        existing_translation = key_response.get('translation')
                        existing_commits.update({source_block_data.data_type: key_commit})
                        is_any_key_updated = True

                if is_any_key_updated:
                    self._update_translations_in_db(
                        translation_obj, existing_translation, existing_commits, source_block_data, target_language_code, key_status,
                    )
                else:
                    translation_obj.last_fetched = datetime.now()
                    translation_obj.save()
                    self._update_result_list(
                        str(translation_obj.target_block.block_id), source_block_data.data_type, target_language_code,
                        "Successfully fetched but commits are same", key_status,
                    )

    def _update_response_translations_in_db(self, data_dict, responses):
        """
        Update WikiTranslations with fetched translated response from meta-server

        Arguments:
            data_dict (Dict): contains request data that used for Send API call
            responses (list): fetched responses list

        Sample data_dict:
        i.e for source block => block-v1:edX+fresh1+fresh1+type@chapter+block@a7e862b4a8b34c8f9c4870b44cbed97b
        {
            "block-v1:edX+fresh1+fresh1+type@chapter+block@a7e862b4a8b34c8f9c4870b44cbed97b": {
                "source_course_id": "source_course_id_1",
                "source_course_lang_code": "en",
                "target_block_versions" : {
                    "hi": "block-v1:edX fresh1 hindi_ver2+type@chapter+block@a7e862b4a8b34c8f9c4870b44cbed97b",
                }
            }
            ...
        }
        Sample responses:
        [
            {
                'response_source_block': "block-v1:edX+fresh1+fresh1+type@chapter+block@a7e862b4a8b34c8f9c4870b44cbed97b",
                'mclanguage': "hi",
                'response_data':  {
                    "display_name": {
                        "key": "Course-v1:edX+fresh1+fresh1/en/block-v1:edX+fresh1+fresh1+type@problem+block@9eefa6c9923346b1b746988401c638ad/display_name",
                        "translation": "चेक बॉक्",
                        "properties": {
                            "status": "translated",
                            "last-translator-text": "wikimeta-translator-username",
                            "last-translator-id": "wikimeta-translator-userid",
                        },
                        "title": "Translations:Course-v1:edX+fresh1+fresh1/en/block-v1:edX+fresh1+fresh1+type@problem+block@9eefa6c9923346b1b746988401c638ad/display_name/hi",
                        "targetLanguage": "hi",
                        "primaryGroup": "messagebundle-Course-v1:edX+fresh1+fresh1/en/block-v1:edX+fresh1+fresh1+type@problem+block@9eefa6c9923346b1b746988401c638ad",
                    }
                }
            }
        ]
        """
        self._UPDATED_TRANSLATIONS = []
        for response in responses:
            if not response:
                continue;

            response_source_block = response.get('response_source_block')
            target_language_code = response.get('mclanguage')
            response_data = response.get('response_data')
            target_block_id = data_dict.get(response_source_block, {}).get('target_block_versions', {}).get(
                target_language_code
            )

            if not response_source_block or not response_source_block or not response_source_block or not target_block_id:
                log.error("Error in updating translations in db due to invalid response or data_dict.")
                log.error(
                    "Response details => response_source_block: {}, target_language_code: {}, response_data: {}".format(
                        response_source_block, response_source_block, response_source_block
                    )
                )
                continue;

            self._check_and_update_translations(response_data, target_block_id, target_language_code)

    def _get_tasks_to_fetch_data_from_wiki_meta(self, data_dict, meta_client, session):
        """
        Returns list of tasks - required for Async API calls of Meta Wiki to fetch translations.
        """
        tasks = []
        for source_block_key, source_block_mapping in data_dict.items():
            source_course_id = source_block_mapping.get("source_course_id")
            source_lang_code = source_block_mapping.get("source_course_lang_code")
            for target_lang_code in source_block_mapping.get("target_block_versions", {}).keys():
                mcgroup = "{}/{}/{}".format(source_course_id, source_lang_code, source_block_key)
                mclanguage = target_lang_code
                tasks.append(
                    meta_client.sync_translations(
                        mcgroup, mclanguage, session
                    )
                )
        return tasks

    async def _request_meta_tasks_in_parallel(self, tasks, limit=3):
        """
        Executes tasks in parallel in a sequential manner.
        It devides tasks in sub-tasks with length equals to limit and call subtasks in parallel.
        """
        responses = []
        for index in range(0, len(tasks), limit):
            sub_tasks = tasks[index:index+limit]
            sub_responses = await asyncio.gather(*sub_tasks)
            responses.extend(sub_responses)
        return responses

    async def async_fetch_data_from_wiki_meta(self, data_dict):
        """
        Async calls to fetch tramslations for course blocks.
        """
        responses = []
        async with aiohttp.ClientSession() as session:
            meta_client = WikiMetaClient()
            tasks = self._get_tasks_to_fetch_data_from_wiki_meta(data_dict, meta_client, session)
            responses = await self._request_meta_tasks_in_parallel(
                tasks,
                limit=meta_client._API_GET_REQUEST_SYNC_LIMIT,
            )
            self._update_response_translations_in_db(data_dict, responses)

    def update_transalted_status_of_updated_blocks(self):
        """
        Updates translated status of Updated blocks
        """
        if self._UPDATED_BLOCKS:
            course_blocks = CourseBlock.objects.filter(block_id__in=list(self._UPDATED_BLOCKS))
            for block in course_blocks:
                is_translated = is_block_translated(block)
                if block.translated != is_translated:
                    block.translated = is_block_translated(block)
                    block.save()
                    
                    log.info(
                        "CourseBlock translated status have been updated for block_id {} translated: {}".format(
                            block.block_id, is_translated,
                        )
                    )
    
    def update_info(self):
        """
        Adds entry to MetaCronJobInfo
        """
        try:
            latest_info = MetaCronJobInfo.objects.latest('change_date')
            MetaCronJobInfo.objects.create(fetched_date = datetime.now(), sent_date = latest_info.sent_date)
        except MetaCronJobInfo.DoesNotExist:
            MetaCronJobInfo.objects.create(fetched_date = datetime.now())
            
    def handle(self, *args, **options):
        data_dict = self._get_request_data_dict()

        if options.get('commit'):
            if data_dict:
                asyncio.run(self.async_fetch_data_from_wiki_meta(data_dict))
                self.update_transalted_status_of_updated_blocks()
            else:
                log.info("No Translations need to fetched/updated from Meta Wiki.")

            self.update_info()
            self._log_final_report(data_dict)
        else:
            log.info(json.dumps(data_dict, indent=4))
