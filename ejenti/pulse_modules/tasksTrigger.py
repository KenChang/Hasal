import os
import re
import copy
import json
import socket
import hashlib
import logging
import urllib2
import urlparse
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

from lib.helper.generateBackfillTableHelper import GenerateBackfillTableHelper
from lib.modules.build_information import BuildInformation
from lib.common.statusFileCreator import StatusFileCreator
from lib.common.commonUtil import CommonUtil
from hasal_consumer import HasalConsumer
from hasalPulsePublisher import HasalPulsePublisher


class TasksTrigger(object):
    """
    MD5_HASH_FOLDER is ".md5"
    """
    ARCHIVE_ROOT_URL = 'https://archive.mozilla.org'
    ARCHIVE_LATEST_FOLDER = '/pub/firefox/nightly/latest-mozilla-central/'
    ARCHIVE_LINK_RE_STRING = r'(?<=href=").*?(?=")'

    KEY_CONFIG_PULSE_USER = 'pulse_username'
    KEY_CONFIG_PULSE_PWD = 'pulse_password'

    KEY_CONFIG_SUITE_WHITE_LIST_FOR_SIGNATURE = 'suite_white_list_for_signature'

    KEY_CONFIG_JOBS = 'jobs'
    KEY_JOBS_ENABLE = 'enable'
    KEY_JOBS_AMOUNT = 'amount'
    KEY_JOBS_TOPIC = 'topic'
    KEY_JOBS_PLATFORM_BUILD = 'platform_build'
    KEY_JOBS_INTERVAL_MINUTES = 'interval_minutes'
    KEY_JOBS_CMD = 'cmd'
    KEY_JOBS_CONFIGS = 'configs'

    MD5_HASH_FOLDER = '.md5'
    TIMESTAMP_FOLDER = '.timestamp'

    # filename example: 'firefox-56.0a1.en-US.linux-x86_64.json'
    MATCH_FORMAT = '.{platform_key}.{ext}'

    PLATFORM_MAPPING = {
        'linux32': {
            'key': 'linux-i686',
            'ext': 'tar.bz2'
        },
        'linux64': {
            'key': 'linux-x86_64',
            'ext': 'tar.bz2'
        },
        'mac': {
            'key': 'mac',
            'ext': 'dmg'
        },
        'win32': {
            'key': 'win32',
            'ext': 'zip'
        },
        'win64': {
            'key': 'win64',
            'ext': 'zip'
        }
    }

    # default query back fill days
    BACK_FILL_DEFAULT_QUERY_DAYS = 14

    def __init__(self, config, cmd_config_obj, clean_at_begin=False):
        self.all_config = config
        self.cmd_config_obj = cmd_config_obj

        # get jobs config
        self.jobs_config = config.get(TasksTrigger.KEY_CONFIG_JOBS, {})
        self.pulse_username = config.get(TasksTrigger.KEY_CONFIG_PULSE_USER)
        self.pulse_password = config.get(TasksTrigger.KEY_CONFIG_PULSE_PWD)
        self.suite_white_list_for_signature = config.get(TasksTrigger.KEY_CONFIG_SUITE_WHITE_LIST_FOR_SIGNATURE, [])

        self._validate_data()

        if clean_at_begin:
            self.clean_pulse_queues()

        self.scheduler = BackgroundScheduler()
        self.scheduler.start()

    def _validate_data(self):
        # validate Pulse account
        if not self.pulse_username or not self.pulse_password:
            # there is no Pulse account information in "job_config.json"
            raise Exception('Cannot access Pulse due to there is no Pulse account information.')

    def clean_pulse_queues(self):
        """
        Cleaning and re-creating enabled Pulse Queues for cleaning Dead Consumer Client on Pulse.
        Dead Consumer Client will get messages without ack(), so messages will always stay on Pulse, and no one can handle it.
        """
        logging.info('Cleaning and re-creating Pulse Queues ...')
        queues_set = set()
        for job_name, job_detail in self.jobs_config.items():
            # have default config
            enable = job_detail.get(TasksTrigger.KEY_JOBS_ENABLE, False)
            topic = job_detail.get(TasksTrigger.KEY_JOBS_TOPIC, '')
            if enable and topic:
                queues_set.add(topic)
        logging.info('Enabled Pulse Queues: {}'.format(queues_set))

        for topic in queues_set:
            ret = HasalPulsePublisher.re_create_pulse_queue(username=self.pulse_username,
                                                            password=self.pulse_password,
                                                            topic=topic)
            if not ret:
                logging.error('Queue [{}] has been deleted, but not be re-created successfully.'.format(topic))
        logging.info('Clean and re-create Pulse Queues done.')

    @staticmethod
    def get_current_file_folder():
        return os.path.dirname(os.path.realpath(__file__))

    @staticmethod
    def check_folder(checked_folder):
        """
        Checking folder.
        @param checked_folder:
        @return: Return True if folder already exists and is folder, or re-create folder successfully.
        """
        try:
            if os.path.exists(checked_folder):
                if os.path.isfile(checked_folder):
                    os.remove(checked_folder)
                    os.makedirs(checked_folder)
                return True
            else:
                # there is no valid MD5 folder
                os.makedirs(checked_folder)
                return True
        except Exception as e:
            logging.error(e)
            return False

    @staticmethod
    def _validate_job_config(job_config):
        """
        Validate the job config. Required keys: topic, platform_build, and cmd.
        @param job_config: job detail config.
        @return: True or False.
        """
        required_keys = [TasksTrigger.KEY_JOBS_TOPIC,
                         TasksTrigger.KEY_JOBS_PLATFORM_BUILD,
                         TasksTrigger.KEY_JOBS_CMD]

        for required_key in required_keys:
            if required_key not in job_config:
                logging.error('There is no required key [{}] in job config.'.format(required_key))
                return False
        return True

    @staticmethod
    def get_all_latest_files_for_md5():
        """
        Get all latest files from ARCHIVE server.
        @return: dict object {'<filename>': '<folder/path/with/filename>', ...}
        """
        latest_url = urlparse.urljoin(TasksTrigger.ARCHIVE_ROOT_URL, TasksTrigger.ARCHIVE_LATEST_FOLDER)
        ret_dict = {}
        try:
            res_obj = urllib2.urlopen(latest_url)
            if res_obj.getcode() == 200:
                for line in res_obj.readlines():
                    match = re.search(TasksTrigger.ARCHIVE_LINK_RE_STRING, line)
                    if match:
                        href_link = match.group(0)
                        name = href_link.split('/')[-1]
                        ret_dict[name] = href_link
            else:
                logging.error('Fetch builds failed. Code: {code}, Link: {link}'.format(code=res_obj.getcode(),
                                                                                       link=latest_url))
        except Exception as e:
            logging.error(e)
        return ret_dict

    @staticmethod
    def get_latest_info_json_url_for_md5(platform):
        """
        Get latest platform build's JSON file URL base on specify platform.
        @param platform: the specify platform. Defined in PLATFORM_MAPPING[<name>]['key'].
        @return: the latest platform build's JSON file URL.
        """
        ext_json = 'json'
        match_endswith_string = TasksTrigger.MATCH_FORMAT.format(platform_key=platform, ext=ext_json)

        # get latest files
        all_files = TasksTrigger.get_all_latest_files_for_md5()

        # find the matched files base on platform, e.g. "win64.json"
        matched_files = {k: v for k, v in all_files.items() if k.endswith(match_endswith_string)}

        if len(matched_files) >= 1:
            # when get matched files, then get the latest file URL folder path
            matched_filename = sorted(matched_files.keys())[-1]
            ret_url = matched_files.get(matched_filename)
            return urlparse.urljoin(TasksTrigger.ARCHIVE_ROOT_URL, ret_url)
        else:
            logging.error('There is no matched filename endswith "{}".'.format(match_endswith_string))
            return None

    @staticmethod
    def get_remote_md5(url, max_size=1 * 1024 * 1024):
        """
        Get remote resource's MD5 hash string.
        @param url: remote resource URL.
        @param max_size: max download size. default is 1*1024*1024 bytes (1 MB).
        @return: the MD5 hash string (lowercase).
        """
        remote_resource = urllib2.urlopen(url)
        md5_handler = hashlib.md5()
        counter = 0
        while True:
            data = remote_resource.read(1024)
            counter += 1024

            if not data or counter >= max_size:
                break
            md5_handler.update(data)
        return md5_handler.hexdigest()

    @staticmethod
    def get_latest_info_json_md5_hash(platform):
        """
        Get MD5 hash string of latest platform build's JSON file base on specify platform.
        @param platform: the specify platform. Defined in PLATFORM_MAPPING[<name>]['key'].
        @return: the MD5 hash string of latest platform build's JSON file.
        """
        json_file_url = TasksTrigger.get_latest_info_json_url_for_md5(platform)
        hash_string = TasksTrigger.get_remote_md5(json_file_url)
        return hash_string

    @staticmethod
    def check_latest_info_json_md5_changed(job_name, platform):
        """
        @param job_name: the job name which will set as identify name.
        @param platform: the platform archive server.
        @return: True if changed, False if not changed.
        """
        md5_folder = os.path.join(TasksTrigger.get_current_file_folder(), TasksTrigger.MD5_HASH_FOLDER)

        # prepare MD5 folder
        if not TasksTrigger.check_folder(md5_folder):
            return False

        # get new MD5 hash
        new_hash = TasksTrigger.get_latest_info_json_md5_hash(platform)

        # check MD5 file
        job_md5_file = os.path.join(md5_folder, job_name)
        if os.path.exists(job_md5_file):
            with open(job_md5_file, 'r') as f:
                origin_hash = f.readline()

            if origin_hash == new_hash:
                # no changed
                return False
            else:
                # changed
                logging.info('Job "{}" platform "{}": Latest Hash [{}], Origin Hash: [{}]'.format(job_name,
                                                                                                  platform,
                                                                                                  new_hash,
                                                                                                  origin_hash))
                with open(job_md5_file, 'w') as f:
                    f.write(new_hash)
                return True
        else:
            # found the file for the 1st time
            logging.info('Job "{}" platform "{}": Latest Hash [{}], no origin hash.'.format(job_name,
                                                                                            platform,
                                                                                            new_hash))
            with open(job_md5_file, 'w') as f:
                f.write(new_hash)
            return True

    @staticmethod
    def clean_md5_by_job_name(job_name):
        """
        clean the md5 file by job name.
        @param job_name: the job name which will set as identify name.
        """
        md5_folder = os.path.join(TasksTrigger.get_current_file_folder(), TasksTrigger.MD5_HASH_FOLDER)

        # prepare MD5 folder
        if not TasksTrigger.check_folder(md5_folder):
            return False

        # check MD5 file
        job_md5_file = os.path.join(md5_folder, job_name)
        if os.path.exists(job_md5_file):
            if os.path.isfile(job_md5_file):
                try:
                    os.remove(job_md5_file)
                    return True
                except Exception as e:
                    logging.error(e)
                    return False
            else:
                logging.warn('The {} is not a file.'.format(job_md5_file))
                return False
        else:
            logging.debug('The {} not exists.'.format(job_md5_file))
            return True

    @staticmethod
    def job_pushing_meta_task_md5(username, password, command_config, job_name, topic, amount, platform_build, cmd_name, overwrite_cmd_config=None):
        """
        [JOB]
        Currently we do not use MD5, we use Timestamp from Archive/Perfherder.
        Pushing the MetaTask if the remote build's MD5 was changed.
        @param username: Pulse username.
        @param password: Pulse password.
        @param command_config: The overall command config dict object.
        @param job_name: The job name which be defined in trigger_config.json.
        @param topic: The Topic on Pulse. Refer to `get_topic()` method of `jobs.pulse`.
        @param amount: The MetaTask amount per time.
        @param platform_build: The platform on Archive server.
        @param cmd_name: The MetaTask command name.
        @param overwrite_cmd_config: The overwrite command config.
        """
        changed = TasksTrigger.check_latest_info_json_md5_changed(job_name=job_name, platform=platform_build)
        if changed:
            # check queue
            queue_exists = HasalPulsePublisher.check_pulse_queue_exists(username=username,
                                                                        password=password,
                                                                        topic=topic)
            if not queue_exists:
                logging.error('There is not Queue for Topic [{topic}]. Message might be ignored.'.format(topic=topic))

            # Push MetaTask to Pulse
            publisher = HasalPulsePublisher(username=username,
                                            password=password,
                                            command_config=command_config)

            now = datetime.now()
            now_string = now.strftime('%Y-%m-%d_%H:%M:%S.%f')
            uid_prefix = '{time}.{job}'.format(time=now_string, job=job_name)
            # push meta task
            logging.info('Pushing to Pulse...\n'
                         '{line}\n'
                         'UID prefix: {uid_prefix}\n'
                         'Trigger Job: {job_name}\n'
                         'Platform: {platform}\n'
                         'Topic: {topic}\n'
                         'Amount: {amount}\n'
                         'command {cmd}\n'
                         'cmd_config: {cmd_config}\n'
                         '{line}\n'.format(uid_prefix=uid_prefix,
                                           job_name=job_name,
                                           platform=platform_build,
                                           topic=topic,
                                           amount=amount,
                                           cmd=cmd_name,
                                           cmd_config=overwrite_cmd_config,
                                           line='-' * 10))
            for idx in range(amount):
                uid = '{prefix}.{idx}'.format(prefix=uid_prefix, idx=idx + 1)
                publisher.push_meta_task(topic=topic,
                                         command_name=cmd_name,
                                         overwrite_cmd_configs=overwrite_cmd_config,
                                         uid=uid)

    @staticmethod
    def clean_timestamp_by_job_name(job_name):
        """
        clean the timestamp file by job name.
        @param job_name: the job name which will set as identify name.
        """
        timestamp_folder = os.path.join(TasksTrigger.get_current_file_folder(), TasksTrigger.TIMESTAMP_FOLDER)

        # prepare timestamp folder
        if not TasksTrigger.check_folder(timestamp_folder):
            return False

        # check timestamp file
        job_timestamp_file = os.path.join(timestamp_folder, job_name)
        if os.path.exists(job_timestamp_file):
            if os.path.isfile(job_timestamp_file):
                try:
                    os.remove(job_timestamp_file)
                    return True
                except Exception as e:
                    logging.error(e)
                    return False
            else:
                logging.warn('The {} is not a file.'.format(job_timestamp_file))
                return False
        else:
            logging.debug('The {} not exists.'.format(job_timestamp_file))
            return True

    @staticmethod
    def check_latest_timestamp(job_name, platform):
        """
        @param job_name: the job name which will set as identify name.
        @param platform: the platform archive server.
        @return: (True, BuildInfomation) if changed, (False, None) if not changed.
        """
        backfill_table_obj = GenerateBackfillTableHelper.get_history_archive_perfherder_relational_table(input_platform=platform)

        if backfill_table_obj:
            latest_timestamp = sorted(backfill_table_obj.keys())[-1]

            timestamp_folder = os.path.join(TasksTrigger.get_current_file_folder(), TasksTrigger.TIMESTAMP_FOLDER)

            # prepare timestamp folder
            if not TasksTrigger.check_folder(timestamp_folder):
                return False, None

            job_timestamp_file = os.path.join(timestamp_folder, job_name)
            if os.path.exists(job_timestamp_file):
                with open(job_timestamp_file, 'r') as f:
                    original_timestamp = f.readline()

                if original_timestamp == latest_timestamp:
                    # no changed
                    return False, None
                else:
                    # changed
                    logging.info('Job "{}" platform "{}": Latest timestamp [{}], Origin timestamp: [{}]'.format(job_name,
                                                                                                                platform,
                                                                                                                latest_timestamp,
                                                                                                                original_timestamp))
                    with open(job_timestamp_file, 'w') as f:
                        f.write(latest_timestamp)
                    return True, BuildInformation(backfill_table_obj.get(latest_timestamp))
            else:
                # found the file for the 1st time
                logging.info('Job "{}" platform "{}": Latest timestamp [{}], no origin timestamp.'.format(job_name,
                                                                                                          platform,
                                                                                                          latest_timestamp))
                with open(job_timestamp_file, 'w') as f:
                    f.write(latest_timestamp)
                return True, BuildInformation(backfill_table_obj.get(latest_timestamp))

        else:
            logging.error('Cannot retrieve the archive relational table of platform: {platform}'.format(platform=platform))
            return False, None

    @staticmethod
    def filter_cmd_config(input_config):
        """
        Mask the command config information, and filter some other information
        @param input_config:
        @return: the modified config
        """
        ret_config = copy.deepcopy(input_config)
        for config_key, config_value in ret_config.items():

            # convert case list
            if config_key == 'OVERWRITE_HASAL_SUITE_CASE_LIST':
                if isinstance(config_value, list):
                    case_list = config_value
                else:
                    case_list = config_value.split(",")
                ret_config[config_key] = case_list

            # convert secret information
            for_record_status_config = copy.deepcopy(ret_config)
            ret_config = CommonUtil.mask_credential_value(for_record_status_config)
        return ret_config

    @staticmethod
    def handle_specify_commands(cmd_name, cmd_configs, build_info):
        """
        Return modified cmd_configs base on specify commands
        @param cmd_name:
        @param cmd_configs:
        @param build_info:
        @return:
        """
        if cmd_name in ['run-hasal-on-specify-nightly', 'download-specify-nightly']:
            # above commands need more information
            cmd_configs['DOWNLOAD_PKG_DIR_URL'] = build_info.archive_url
            # additional informaion for tracing
            cmd_configs['DOWNLOAD_REVISION'] = build_info.revision
        return cmd_configs

    @staticmethod
    def job_pushing_meta_task(username, password, command_config, job_name, topic, amount, platform_build, cmd_name, overwrite_cmd_config=None):
        """
        [JOB]
        Pushing the MetaTask if the remote build's MD5 was changed.
        @param username: Pulse username.
        @param password: Pulse password.
        @param command_config: The overall command config dict object.
        @param job_name: The job name which be defined in trigger_config.json.
        @param topic: The Topic on Pulse. Refer to `get_topic()` method of `jobs.pulse`.
        @param amount: The MetaTask amount per time.
        @param platform_build: The platform on Archive server.
        @param cmd_name: The MetaTask command name.
        @param overwrite_cmd_config: The overwrite command config.
        """
        logging.info('checking Job [{}], Platform [{}]...'.format(job_name, platform_build))
        changed, build_info = TasksTrigger.check_latest_timestamp(job_name=job_name, platform=platform_build)
        logging.info('checking Job [{}], Platform [{}]... {}'.format(job_name, platform_build, changed))

        if changed:
            # prepare job id status folder
            job_id = StatusFileCreator.create_job_id_folder(job_name)
            job_id_fp = os.path.join(StatusFileCreator.get_status_folder(), job_id)
            # Recording Status
            StatusFileCreator.create_status_file(job_id_fp, StatusFileCreator.STATUS_TAG_PULSE_TRIGGER, 100)

            # check queue
            queue_exists = HasalPulsePublisher.check_pulse_queue_exists(username=username,
                                                                        password=password,
                                                                        topic=topic)
            if not queue_exists:
                logging.error('There is not Queue for Topic [{topic}]. Message might be ignored.'.format(topic=topic))

            # Pre-handle specify command
            overwrite_cmd_config = TasksTrigger.handle_specify_commands(cmd_name, overwrite_cmd_config, build_info)

            # Push MetaTask to Pulse
            publisher = HasalPulsePublisher(username=username,
                                            password=password,
                                            command_config=command_config)

            now = datetime.now()
            now_string = now.strftime('%Y-%m-%d_%H:%M:%S.%f')
            uid_prefix = '{time}.{job}'.format(time=now_string, job=job_name)
            # push meta task
            logging.info('Pushing to Pulse...\n'
                         '{line}\n'
                         'UID prefix: {uid_prefix}\n'
                         'Trigger Job: {job_name}\n'
                         'Platform: {platform}\n'
                         'Topic: {topic}\n'
                         'Amount: {amount}\n'
                         'command {cmd}\n'
                         'cmd_config: {cmd_config}\n'
                         '{line}\n'.format(uid_prefix=uid_prefix,
                                           job_name=job_name,
                                           platform=platform_build,
                                           topic=topic,
                                           amount=amount,
                                           cmd=cmd_name,
                                           cmd_config=overwrite_cmd_config,
                                           line='-' * 10))
            uid_list = []
            for idx in range(amount):
                uid = '{prefix}.{idx}'.format(prefix=uid_prefix, idx=idx + 1)
                uid_list.append(uid)
                publisher.push_meta_task(topic=topic,
                                         command_name=cmd_name,
                                         overwrite_cmd_configs=overwrite_cmd_config,
                                         uid=uid)

            # Recording Status
            content = {
                'job_name': job_name,
                'platform': platform_build,
                'topic': topic,
                'amount': amount,
                'cmd': cmd_name,
                'cmd_config': TasksTrigger.filter_cmd_config(overwrite_cmd_config),
                'task_uid_list': uid_list
            }
            StatusFileCreator.create_status_file(job_id_fp, StatusFileCreator.STATUS_TAG_PULSE_TRIGGER, 900, content)

    @staticmethod
    def get_enabled_platform_list_from_trigger_jobs_config(config_dict_obj):
        """
        Return the list which contains enabled platforms.
        Note:
        - Default enable "win64" platform builds. So, cases progress dashboard can also use this information.
        @param config_dict_obj: the jobs dict object in trigger config file
        @return: list
        """
        enabled_platform_set = set()

        # Default enable platform build "win64"
        enabled_platform_set.add('win64')

        for _, job_detail in config_dict_obj.items():
            enable = job_detail.get(TasksTrigger.KEY_JOBS_ENABLE, False)
            platform_build = job_detail.get(TasksTrigger.KEY_JOBS_PLATFORM_BUILD)
            if enable and platform_build:
                enabled_platform_set.add(platform_build)
        logging.info('Enabled platforms: {}'.format(enabled_platform_set))
        return list(enabled_platform_set)

    @staticmethod
    def job_listen_response_from_agent(username, password, rotating_file_path):
        """
        [JOB]
        Logging the message from Agent by Pulse "mgt" topic channel.
        @param username: Pulse username.
        @param password: Pulse password.
        @param rotating_file_path: The rotating file path.
        """
        PULSE_MGT_TOPIC = 'mgt'
        PULSE_MGT_OBJECT_KEY = 'message'

        rotating_logger = logging.getLogger("RotatingLog")
        rotating_logger.setLevel(logging.INFO)

        # create Rotating File Handler, 1 day, backup 30 times.
        rotating_handler = TimedRotatingFileHandler(rotating_file_path,
                                                    when='midnight',
                                                    interval=1,
                                                    backupCount=30)

        rotating_formatter = logging.Formatter('%(asctime)s, %(levelname)s, %(message)s')
        rotating_handler.setFormatter(rotating_formatter)
        rotating_logger.addHandler(rotating_handler)

        def got_response(body, message):
            """
            handle the message
            ack then broker will remove this message from queue
            """
            message.ack()
            data_payload = body.get('payload')
            msg_dict_obj = data_payload.get(PULSE_MGT_OBJECT_KEY)
            try:
                msg_str = json.dumps(msg_dict_obj)
                rotating_logger.info(msg_str)
            except:
                rotating_logger.info(msg_dict_obj)

        hostname = socket.gethostname()
        consumer_label = 'TRIGGER-{hostname}'.format(hostname=hostname)
        topic = PULSE_MGT_TOPIC
        c = HasalConsumer(user=username, password=password, applabel=consumer_label)
        c.configure(topic=topic, callback=got_response)

        c.listen()

    def _job_exception_listener(self, event):
        if event.exception:
            logging.error("Job [%s] crashed [%s]" % (event.job_id, event.exception))
            logging.error(event.traceback)

    def _add_event_listener(self):
        self.scheduler.add_listener(self._job_exception_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    def run(self, skip_first_query=False):
        """
        Adding jobs into scheduler.
        """
        # add event listener
        self._add_event_listener()

        # create "mgt" channel listener
        logging.info('Adding Rotating Logger for listen Agent information ...')
        MGT_ID = 'trigger_mgt_listener'
        MGT_LOG_PATH = 'rotating_mgt.log'
        self.scheduler.add_job(func=TasksTrigger.job_listen_response_from_agent,
                               trigger='interval',
                               id=MGT_ID,
                               max_instances=1,
                               seconds=10,
                               args=[],
                               kwargs={'username': self.pulse_username,
                                       'password': self.pulse_password,
                                       'rotating_file_path': MGT_LOG_PATH})
        logging.info('Adding Rotating Logger done: {fp}'.format(fp=os.path.abspath(MGT_LOG_PATH)))

        # loading enabled platform list
        enabled_platform_list = TasksTrigger.get_enabled_platform_list_from_trigger_jobs_config(self.jobs_config)

        # 1st time generating back fill table for query
        if not skip_first_query:
            for platform_build in enabled_platform_list:
                logging.info('Generating latest [{}] backfill table ...'.format(platform_build))
                GenerateBackfillTableHelper.generate_archive_perfherder_relational_table(
                    input_backfill_days=TasksTrigger.BACK_FILL_DEFAULT_QUERY_DAYS, input_platform=platform_build,
                    input_white_list=self.suite_white_list_for_signature)
            logging.info('Generating latest backfill tables done.')

        # creating jobs for query backfill table
        for platform_build in enabled_platform_list:
            self.scheduler.add_job(func=GenerateBackfillTableHelper.generate_archive_perfherder_relational_table,
                                   trigger='interval',
                                   id='query_backfill_table_{}'.format(platform_build),
                                   max_instances=1,
                                   minutes=10,
                                   args=[],
                                   kwargs={'input_backfill_days': TasksTrigger.BACK_FILL_DEFAULT_QUERY_DAYS,
                                           'input_platform': platform_build,
                                           'input_white_list': self.suite_white_list_for_signature})

        # create each Trigger jobs
        for job_name, job_detail in self.jobs_config.items():
            """
            ex:
            {
                "win7_x64": {
                    "enable": true,
                    "topic": "win7",
                    "platform_build": "win64",
                    "interval_minutes": 10,
                    "cmd": "download-latest-nightly",
                    "configs": {}
                },
                ...
            }
            """
            if not TasksTrigger._validate_job_config(job_detail):
                logging.error('There is not valid job.\n{}: {}\n'.format(job_name, job_detail))

            # have default config
            enable = job_detail.get(TasksTrigger.KEY_JOBS_ENABLE, False)
            interval_minutes = job_detail.get(TasksTrigger.KEY_JOBS_INTERVAL_MINUTES, 10)
            configs = job_detail.get(TasksTrigger.KEY_JOBS_CONFIGS, {})
            amount = job_detail.get(TasksTrigger.KEY_JOBS_AMOUNT, 1)
            # required
            topic = job_detail.get(TasksTrigger.KEY_JOBS_TOPIC)
            platform_build = job_detail.get(TasksTrigger.KEY_JOBS_PLATFORM_BUILD)
            cmd = job_detail.get(TasksTrigger.KEY_JOBS_CMD)

            if enable:
                logging.info('Job [{}] is enabled.'.format(job_name))

                # adding Job Trigger
                self.scheduler.add_job(func=TasksTrigger.job_pushing_meta_task,
                                       trigger='interval',
                                       id=job_name,
                                       max_instances=1,
                                       minutes=interval_minutes,
                                       args=[],
                                       kwargs={'username': self.pulse_username,
                                               'password': self.pulse_password,
                                               'command_config': self.cmd_config_obj,
                                               'job_name': job_name,
                                               'topic': topic,
                                               'amount': amount,
                                               'platform_build': platform_build,
                                               'cmd_name': cmd,
                                               'overwrite_cmd_config': configs})

            else:
                logging.info('Job [{}] is disabled.'.format(job_name))
