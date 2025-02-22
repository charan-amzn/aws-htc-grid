# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# Licensed under the Apache License, Version 2.0 https://aws.amazon.com/apache-2-0/

import boto3
import botocore
import json
import time
import os
import logging
import subprocess
import traceback
import random
import signal
import sys
import base64
import asyncio
import requests
from functools import partial
from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch
from aws_xray_sdk import global_sdk_config

from botocore.exceptions import ClientError
from botocore.config import Config
import utils.dynamodb_common as ddb
from api.in_out_manager import in_out_manager
from utils.performance_tracker import EventsCounter, performance_tracker_initializer
import utils.grid_error_logger as errlog
from utils.ttl_experation_generator import TTLExpirationGenerator

# Uncomment to get tracing on interruption
# import faulthandler
# faulthandler.enable(file=sys.stderr, all_threads=True)


logging.basicConfig(
    format="{ \"filename\" : \"%(filename).7s.py\", "
           "\"functionName\" : \"%(funcName).5s\", "
           "\"line\" : \"%(lineno)4d\" , "
           "\"time\" : \"%(asctime)s\","
           "\"level\":\"%(levelname)5s\","
           "\"message\" : \"%(message)s\" }",
    datefmt='%H:%M:%S', level=logging.INFO)

logging.getLogger('aws_xray_sdk').setLevel(logging.DEBUG)

# Uncomment to get DEBUG logging
# boto3.set_stream_logger('', logging.DEBUG)

rand_delay = random.randint(5, 15)
logging.info("SLEEP DELAY {}".format(rand_delay))
time.sleep(rand_delay)

session = boto3.session.Session()

try:
    agent_config_file = os.environ['AGENT_CONFIG_FILE']
except KeyError:
    agent_config_file = "/etc/agent/Agent_config.tfvars.json"

with open(agent_config_file, 'r') as file:
    agent_config_data = json.loads(file.read())

# If there are no tasks in the queue we do not attempt to retrieve new tasks for that interval

empty_task_queue_backoff_timeout_sec = agent_config_data['empty_task_queue_backoff_timeout_sec']
work_proc_status_pull_interval_sec = agent_config_data['work_proc_status_pull_interval_sec']
task_ttl_expiration_offset_sec = agent_config_data['task_ttl_expiration_offset_sec']
task_ttl_refresh_interval_sec = agent_config_data['task_ttl_refresh_interval_sec']
task_input_passed_via_external_storage = agent_config_data['task_input_passed_via_external_storage']
agent_sqs_visibility_timeout_sec = agent_config_data['agent_sqs_visibility_timeout_sec']
USE_CC = agent_config_data['agent_use_congestion_control']
IS_XRAY_ENABLE = agent_config_data['enable_xray']
region = agent_config_data["region"]
# TODO: redirect logs to fluentD

AGENT_EXEC_TIMESTAMP_MS = 0
execution_is_completed_flag = 0

try:
    SELF_ID = os.environ['MY_POD_NAME']
except KeyError:
    SELF_ID = "1234"
    pass

# TODO - retreive the endpoint url from Terraform
sqs = boto3.resource('sqs', endpoint_url=agent_config_data['sqs_endpoint'], region_name=region)
# sqs = boto3.resource('sqs', region_name=region)
tasks_queue = sqs.get_queue_by_name(QueueName=agent_config_data['sqs_queue'])


lambda_cfg = botocore.config.Config(retries={'max_attempts': 3}, read_timeout=2000, connect_timeout=2000,
                                    region_name=region)
lambda_client = boto3.client('lambda', config=lambda_cfg, endpoint_url=os.environ['LAMBDA_ENDPOINT_URL'],
                             region_name=region)


# TODO: We are using two retry logics for accessing DynamoDB config, and config_cc (for congestion control)
# Revisit this code and unify the logic.
config = Config(
    retries={
        'max_attempts': 5,
        'mode': 'standard'
    }
)
dynamodb = boto3.resource('dynamodb', region_name=region, config=config)
status_table = dynamodb.Table(agent_config_data['ddb_status_table'])

config_cc = Config(
    retries={
        'max_attempts': 10,
        'mode': 'adaptive'
    }
)
dynamodb_cc = boto3.resource('dynamodb', region_name=region, config=config_cc)
status_table_cc = dynamodb_cc.Table(agent_config_data['ddb_status_table'])

stdout_iom = in_out_manager(
    agent_config_data['grid_storage_service'],
    agent_config_data['s3_bucket'], agent_config_data['redis_url'],
    s3_region=region)

perf_tracker_pre = performance_tracker_initializer(agent_config_data["metrics_are_enabled"],
                                                   agent_config_data["metrics_pre_agent_connection_string"],
                                                   agent_config_data["metrics_grafana_private_ip"])
event_counter_pre = EventsCounter(["agent_no_messages_in_tasks_queue", "agent_failed_to_claim_ddb_task",
                                   "agent_successful_acquire_a_task", "agent_auto_throtling_event",
                                   "rc_cubic_decrease_event"])

perf_tracker_post = performance_tracker_initializer(agent_config_data["metrics_are_enabled"],
                                                    agent_config_data["metrics_post_agent_connection_string"],
                                                    agent_config_data["metrics_grafana_private_ip"])
event_counter_post = EventsCounter([
    "ddb_set_task_finished_failed", "ddb_set_task_finished_succeeded", "counter_update_ttl",
    "counter_update_ttl_failed", "counter_user_code_ret_code_failed",
    "bootstrap_failure",
    "task_exec_time_ms", "agent_total_time_ms", "str_pod_id"])


class GracefulKiller:
    """
    This class manage graceful termination when pods are terminated
    """
    kill_now = False

    def __init__(self):
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, signum, frame):
        """ This method is called when a signal from the kernel is sent
        Args:
            signum (int) : the id of the signal to catch
            frame (int) :
        Returns:
            Nothing
        """
        self.kill_now = True
        return 0


def get_time_now_ms():
    """This function returns the time in millisecond
    Returns:
        Current Integer time in ms

    """
    return int(round(time.time() * 1000))


ttl_gen = TTLExpirationGenerator(task_ttl_refresh_interval_sec, task_ttl_expiration_offset_sec)

# {'Items': [{'session_size': Decimal('10'), 'submission_timestamp': Decimal('1612276891690'), 'task_id': 'bd88ea18-6564-11eb-b5fb-060372291b89-part007_9', 'task_status': 'processing-part007', 'task_definition': 'passed_via_storage_size_75_bytes', 'task_owner': 'htc-agent-6d54fd8dfd-7wgpk', 'heartbeat_expiration_timestamp': Decimal('1612277256'), 'session_id': 'bd88ea18-6564-11eb-b5fb-060372291b89-part007', 'sqs_handler_id': 'AQEB19gkPrI8MNJlqfdu+kH4Xr/QOnZWvH9E6qcMTVuHOEKZdhvCeGdW3opZ38k5uIngM94MEzaIZyciDpZYNuwNgXozpp2vpRz5x952R80GAt26FsPmuQQoJ6gdm7dJabHqblYghXw8r+92yTdmSZRnzAr7fpkF2f7C6LoP3AEPVa8DV/6MYbrkKBqjeQLWctQmmTwvcqVkIWJH4KqokjMx+WQt1tGHLBrdd8xPwFlb8kGgwq1d6qeu5hHkdTizoaUDqbLShSYhSWlfysZ7r9its9owIkiZiYDc5/SdPKEi2hga9SH7E1GTtKetk9mUgoH2p4lCFdH2jIDnpY5EVHoicyviCWA2AMOolDZrIeTBtPklWXOnw3Wkljr2qtWbCHS7s6R1Qpis82n+5pVJUjoNfA==', 'task_completion_timestamp': Decimal('0'), 'retries': Decimal('1'), 'parent_session_id': 'bd88ea18-6564-11eb-b5fb-060372291b89-part007'}]


def is_task_has_been_cancelled(task_id):
    """
    This function checks if the task's status is cancelled.
    It is possible that the tasks/session were cancelled by the clinet before this task has been
    picked up from SQS. Thus, we failed to ackquire this task from DDB because its status is cancelled.

    Returns:
        True if task's status is cancelled in DDB.
    """

    ddb_response = ddb.read_task_row(status_table, task_id)
    logging.info("RESP:: {}".format(ddb_response))

    if ((ddb_response is not None) and (len(ddb_response['Items']) == 1)):
        if ddb_response['Items'][0]['task_status'].startswith("cancelled"):
            return True

    return False


def try_to_acquire_a_task():
    """
    This function will fetch tasks from the SQS queue one at a time. Once is tasks is polled from the queue, then agent
    will try to acquire the task by a conditional write on dymanoDB. The tasks will be acquired if tasks in dynamoDB
    is set as "pending" and the owner is "None"

    Returns:
        A tuple containing the SQS message and the task definition

    Raises:
        Exception: occurs when task acquisition failed

    """
    global AGENT_EXEC_TIMESTAMP_MS
    logging.info("waiting for SQS message")
    messages = tasks_queue.receive_messages(MaxNumberOfMessages=1, WaitTimeSeconds=10)

    task_pick_up_from_sqs_ms = get_time_now_ms()

    logging.info("try_to_acquire_a_task, message: {}".format(messages))
    # print(len(messages))

    if len(messages) == 0:
        event_counter_pre.increment("agent_no_messages_in_tasks_queue")
        return None, None

    message = messages[0]
    AGENT_EXEC_TIMESTAMP_MS = get_time_now_ms()

    task = json.loads(message.body)
    logging.info("try_to_acquire_a_task, task: {}".format(task))

    # Since we read this message from the queue, now we need to associate an
    # sqs handler with this message, to be able to delete it later
    task["sqs_handle_id"] = message.receipt_handle
    try:
        result, response, error = ddb.claim_task_to_yourself(
            status_table, task, SELF_ID, ttl_gen.generate_next_ttl().get_next_expiration_timestamp())
        logging.info("DDB claim_task_to_yourself result: {} {}".format(result, response))

        if not result:
            event_counter_pre.increment("agent_failed_to_claim_ddb_task")

            if is_task_has_been_cancelled(task["task_id"]):
                logging.info("Task [{}] has been already cancelled, skipping".format(task['task_id']))
                message.delete()
                return None, None

            else:

                time.sleep(random.randint(1, 3))
                return None, None

    except Exception as error_acquiring:
        errlog.log("Releasing msg after failed try_to_acquire_a_task {} [{}]".format(
            error_acquiring, traceback.format_exc()))
        raise error_acquiring
        # if e.response['Error']['Code'] == 'ResourceNotFoundException':
    # If we have succesfully ackquired a message we should change its visibility timeout
    message.change_visibility(VisibilityTimeout=agent_sqs_visibility_timeout_sec)
    task["stats"]["stage3_agent_01_task_acquired_sqs_tstmp"]["tstmp"] = task_pick_up_from_sqs_ms

    task["stats"]["stage3_agent_02_task_acquired_ddb_tstmp"]["tstmp"] = get_time_now_ms()
    event_counter_pre.increment("agent_successful_acquire_a_task")

    return message, task


def process_subprocess_completion(perf_tracker, task, sqs_msg, fname_stdout, stdout=None):
    """
    This function is responsible for updating the dynamoDB item associated to the input task with the ouput of the
    execution
    Args:
        perf_tracker (utils.performance_tracker.PerformanceTracker): endpoint for sending metrics
        task (dict): the task that went to completion
        sqs_msg (Message): the SQS message associated to the completed task
        fname_stdout (file): the file  where stdout was redirected
        stdout (str): the stdout of the execution

    Returns:
        Nothing

    """
    task["stats"]["stage4_agent_01_user_code_finished_tstmp"]["tstmp"] = get_time_now_ms()

    # <1.> Store stdout/stderr into persistent storage
    if stdout is not None:
        b64output = base64.b64encode(stdout.encode("utf-8"))
        stdout_iom.put_output_from_bytes(task["task_id"], data=b64output)
    else:
        stdout_iom.put_output_from_file(task["task_id"], file_name=fname_stdout)
        # logging.info("\n===========STDOUT: ================")
        # logging.info(open(fname_stdout, "r").read())

        # ret = stdout_iom.put_error_from_file(task["task_id"], file_name=fname_stderr)

        # logging.info("\n===========STDERR: ================")
        # logging.info(open(fname_stderr, "r").read())

    task["stats"]["stage4_agent_02_S3_stdout_delivered_tstmp"]["tstmp"] = get_time_now_ms()

    count = 0
    while True:
        count += 1
        time_start_ms = get_time_now_ms()
        ddb_res, response, error = ddb.dynamodb_update_task_status_to_finished(status_table_cc, task, SELF_ID)
        time_end_ms = get_time_now_ms()

        if not ddb_res and error.response['Error']['Code'] in ["ThrottlingException",
                                                               "ProvisionedThroughputExceededException"]:
            errlog.log("Agent FINISHED@DDB #{} Throttling for {} ms".format(count, time_end_ms - time_start_ms))
            continue
        else:
            break

    if not ddb_res:
        # We can get here if task has been taken over by the watchdog lambda
        # in this case we ignore results and proceed to the next task.
        event_counter_post.increment("ddb_set_task_finished_failed")
        logging.info("Could not set completion time to Finish")

    else:
        event_counter_post.increment("ddb_set_task_finished_succeeded")
        logging.info(
            "We have succesfully marked task as completed in dynamodb."
            " Deleting message from the SQS... for task [{}] {}".format(
                task["task_id"], response))
        sqs_msg.delete()

    logging.info("Exec time1: {} {}".format(get_time_now_ms() - AGENT_EXEC_TIMESTAMP_MS, AGENT_EXEC_TIMESTAMP_MS))
    event_counter_post.increment("agent_total_time_ms", get_time_now_ms() - AGENT_EXEC_TIMESTAMP_MS)
    event_counter_post.set("str_pod_id", SELF_ID)

    submit_post_agent_measurements(task, perf_tracker)


def submit_post_agent_measurements(task, perf=None):
    if perf is None:
        perf = perf_tracker_post
    perf.add_metric_sample(task["stats"], event_counter_post,
                           from_event="stage3_agent_02_task_acquired_ddb_tstmp",
                           to_event="stage4_agent_02_S3_stdout_delivered_tstmp")
    perf.submit_measurements()


def submit_pre_agent_measurements(task):
    perf_tracker_pre.add_metric_sample(task["stats"], event_counter_pre,
                                       from_event="stage2_sbmtlmba_02_before_batch_write_tstmp",
                                       to_event="stage3_agent_02_task_acquired_ddb_tstmp")
    perf_tracker_pre.submit_measurements()


async def do_task_local_execution_thread(
        perf_tracker, task, sqs_msg, task_def, f_stdout, f_stderr, fname_stdout):
    global execution_is_completed_flag
    xray_recorder.begin_subsegment('sub-process-1')
    command = ["./mock_compute_engine",
               task_def["worker_arguments"][0],
               task_def["worker_arguments"][1],
               task_def["worker_arguments"][2]]

    print(command)

    proc = subprocess.Popen(
        command,
        stdout=f_stdout,
        stderr=f_stderr,
        shell=False)

    while True:
        retcode = proc.poll()
        if retcode is not None:
            execution_is_completed_flag = 1  # indicate that this thread is completed

            process_subprocess_completion(perf_tracker, task, sqs_msg, fname_stdout)
            xray_recorder.end_subsegment()
            return retcode

        await asyncio.sleep(work_proc_status_pull_interval_sec)


async def do_task_local_lambda_execution_thread(perf_tracker, task, sqs_msg, task_def):
    global execution_is_completed_flag

    t_start = get_time_now_ms()

    # TODO How big of a payload we can pass here?
    payload = json.dumps(task_def).encode()
    xray_recorder.begin_subsegment('lambda')
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None, partial(
            lambda_client.invoke,
            FunctionName=os.environ['LAMBDA_FONCTION_NAME'],
            InvocationType='RequestResponse',
            Payload=payload,
            LogType='Tail'
        )
    )
    logging.info("TASK FINISHED!!!\nRESPONSE: [{}]".format(response))
    logs = base64.b64decode(response['LogResult']).decode('utf-8')
    logging.info("logs : {}".format(logs))

    ret_value = response['Payload'].read().decode('utf-8')
    logging.info("retValue : {}".format(ret_value))

    execution_is_completed_flag = 1

    if "BOOTSTRAP ERROR" in ret_value:
        event_counter_post.increment("bootstrap_failure", 1)
    else:
        event_counter_post.increment("task_exec_time_ms", get_time_now_ms() - t_start)

        process_subprocess_completion(perf_tracker, task, sqs_msg, None, stdout=ret_value)

    xray_recorder.end_subsegment()
    return ret_value


def update_ttl_if_required(task):
    ddb_res = True

    # If this is the first time we are resetting ttl value or
    # If the next time we will come to this point ttl ticket will expire
    if ((ttl_gen.get_next_refresh_timestamp() == 0)
            or (ttl_gen.get_next_refresh_timestamp() < time.time() + work_proc_status_pull_interval_sec)):
        logging.info("***Updating TTL***")
        # event_counter_post.increment("counter_update_ttl")

        count = 0
        while True:
            count += 1
            t1 = get_time_now_ms()

            # Note, if we will timeout on DDB update operation and we have to repeat this loop iteration,
            # we will regenerate a new TTL ofset, which is what we want.
            ddb_res, response, error = ddb.update_own_tasks_ttl(
                status_table_cc, task, SELF_ID, ttl_gen.generate_next_ttl().get_next_expiration_timestamp()
            )

            t2 = get_time_now_ms()

            if not ddb_res and error.response['Error']['Code'] in ["ThrottlingException",
                                                                   "ProvisionedThroughputExceededException"]:
                errlog.log("Agent TTL@DDB Throttling #{} for {} ms".format(count, t2 - t1))
                continue
            else:
                break

    return ddb_res


async def do_ttl_updates_thread(task):
    global execution_is_completed_flag
    logging.info("START TTL-1")
    while not bool(execution_is_completed_flag):
        logging.info("Check TTL")

        ddb_res = update_ttl_if_required(task)

        if not ddb_res:
            event_counter_post.increment("counter_update_ttl_failed")
            logging.info("Could not set TTL Expiration timestamp.")
            submit_post_agent_measurements(task)
            return False

        # We are sleeping for the remaining duration of the HB interval. If for some reason we were delayed by more
        # than our interval then sleep for 0 sec and go ahead (before tasks TTL expired)
        # required_sleep = max(0,
        #                      work_proc_status_pull_interval_sec - (get_time_now_ms() - exec_loop_iter_time_ms)
        #                      / 1000.0)
        required_sleep = work_proc_status_pull_interval_sec
        await asyncio.sleep(required_sleep)


def prepare_arguments_for_execution(task):
    if task_input_passed_via_external_storage == 1:
        execution_payload = stdout_iom.get_input_to_bytes(task["task_id"])
        execution_payload = base64.b64decode(execution_payload)
    else:
        execution_payload = task["task_definition"]

    return execution_payload


async def run_task(task, sqs_msg):
    global execution_is_completed_flag
    xray_recorder.begin_segment('run_task')
    logging.info("Running Task: {}".format(task))
    xray_recorder.begin_subsegment('encoding')
    bin_protobuf = prepare_arguments_for_execution(task)
    tast_str = bin_protobuf.decode("utf-8")
    task_def = json.loads(tast_str)

    submit_pre_agent_measurements(task)
    task_id = task["task_id"]

    fname_stdout = "./stdout-{task_id}.log".format(task_id=task_id)
    fname_stderr = "./stderr-{task_id}.log".format(task_id=task_id)
    f_stdout = open(fname_stdout, "w")
    f_stderr = open(fname_stderr, "w")

    xray_recorder.end_subsegment()
    execution_is_completed_flag = 0

    task_execution = asyncio.create_task(
        do_task_local_lambda_execution_thread(perf_tracker_post, task, sqs_msg, task_def)
    )

    task_ttl_update = asyncio.create_task(do_ttl_updates_thread(task))
    await asyncio.gather(task_execution, task_ttl_update)
    f_stdout.close()
    f_stderr.close()
    xray_recorder.end_segment()
    logging.info("Finished Task: {}".format(task))
    return True


def event_loop():
    logging.info("Starting main event loop")
    killer = GracefulKiller()
    while not killer.kill_now:

        sqs_msg, task = try_to_acquire_a_task()

        if task is not None:
            asyncio.run(run_task(task, sqs_msg))
            logging.info("Back to main loop")
        else:
            timeout = random.uniform(empty_task_queue_backoff_timeout_sec, 2 * empty_task_queue_backoff_timeout_sec)
            logging.info("Could not acquire a task from the queue, backing off for {}".
                         format(timeout)
                         )
            time.sleep(timeout)

    url = "{}/2018-06-01/stop".format(os.environ['LAMBDA_ENDPOINT_URL'])
    r = requests.post(url)
    logging.info("stopped status {}".format(r))
    if r.status_code != 200:
        logging.info("failed stopping the lambda : {}".format(r.json()))
    else:
        # TODO: at some point we need to pass any information that requests body/json throws
        logging.info("lambda successfully stopped")


if __name__ == "__main__":

    try:

        # try_verify_credentials()
        if IS_XRAY_ENABLE == "1":
            global_sdk_config.set_sdk_enabled(True)
            xray_recorder.configure(
                service='ecs',
                context_missing='LOG_ERROR',
                daemon_address='xray-service.kube-system:2000',
                plugins=('EC2Plugin', 'ECSPlugin')
            )
            libs_to_patch = ('boto3', 'requests')
            patch(libs_to_patch)
        else:
            global_sdk_config.set_sdk_enabled(False)

        event_loop()

    except ClientError as e:
        errlog.log("ClientError Agent Event Loop {} [{}] POD:{}".
                   format(e.response['Error']['Code'], traceback.format_exc(), SELF_ID))
        sys.exit(1)

    except Exception as e:
        errlog.log("Exception Agent Event Loop {} [{}] POD:{}".
                   format(e, traceback.format_exc(), SELF_ID))
        sys.exit(1)
