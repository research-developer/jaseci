"""
Module that manage and optimizes the actions configuration of Jaseci
"""
from jaseci.jsorc.jsorc import JsOrc
from jaseci.extens.svc.kube_svc import KubeService
from jaseci.jsorc.remote_actions import ACTIONS_SPEC_LOC
from jaseci.utils.utils import logger
from jaseci.jsorc.live_actions import (
    load_module_actions,
    unload_module,
    unload_remote_actions,
    load_remote_actions,
    live_actions,
    action_configs,
)

import requests
import copy
import time
from collections import OrderedDict
from functools import cmp_to_key

from .actions_state import ActionsState

POLICIES = ["Default", "Evaluation", "Auto"]
THRESHOLD = 0.1
NODE_MEM_THRESHOLD = 0.8
WINDOW_SIZE = 4


class ActionsOptimizer:
    def __init__(
        self,
        namespace: str = "default",
        policy: str = "Default",
        benchmark: dict = {},
        actions_history: dict = {},
        actions_calls: dict = {},
    ) -> None:
        self.actions_state = ActionsState()
        self.actions_change = {}
        self.jsorc_interval = 0
        self.namespace = namespace
        self.policy = policy
        self.benchmark = benchmark
        self.actions_history = actions_history
        self.actions_calls = actions_calls
        self.policy_params = {}
        self.policy_state = {}
        self.last_eval_configs = []

    def kube_create(self, config):
        kube = JsOrc.svc("kube").poke(cast=KubeService)
        for kind, conf in config.items():
            name = conf["metadata"]["name"]
            kube.create(kind, name, conf, kube.namespace, "ActionsOptimzer:")

    def kube_delete(self, config):
        kube = JsOrc.svc("kube").poke(cast=KubeService)
        for kind, conf in config.items():
            name = conf["metadata"]["name"]
            kube.delete(kind, name, kube.namespace, "ActionsOptimzer:")

    def get_actions_status(self, name=""):
        """
        Return the state of action
        """
        if name == "":
            return self.actions_state.get_all_state()
        else:
            return self.actions_state.get_state(name)

    def retire_remote(self, name):
        """
        Retire a microservice through the kube service
        """
        config = action_configs[name]["remote"]
        self.kube_delete(config)
        self.actions_state.remove_remote(name)

    def spawn_remote(self, name):
        """
        Spawn a microservice through the kube service
        """
        config = action_configs[name]["remote"]
        self.kube_create(config)
        url = f"http://{config['Service']['metadata']['name']}/"
        return url

    def call_action(self, action_name, *params):
        """
        Call an action via live_actions
        """
        func = live_actions[action_name]
        func(*params)

    def action_prep(self, name):
        """
        Any action preparation that needs to be called right after action is loaded
        """
        pass

    def load_action_remote(self, name, unload_existing=False):
        """
        Load a remote action.
        JSORC will get the URL of the remote microservice
        and stand up a microservice if there isn't currently one in the cluster.
        Return True if the remote action is loaded successfully,
        False otherwise
        """
        cur_state = self.actions_state.get_state(name)
        if cur_state is None:
            cur_state = self.actions_state.init_state(name)

        if cur_state["mode"] == "remote" and cur_state["remote"]["status"] == "READY":
            # Check if there is already a remote action loaded
            logger.info("Already load as remote")
            return True

        url = self.actions_state.get_remote_url(name)
        if url is None:
            # Spawn a remote microservice
            url = self.spawn_remote(name)
            self.actions_state.start_remote_service(name, url)
            cur_state = self.actions_state.get_state(name)

        if cur_state["remote"]["status"] == "STARTING":
            if_ready = self.remote_action_ready_check(name, url)
            if if_ready:
                self.actions_state.set_remote_action_ready(name)
                cur_state = self.actions_state.get_state(name)

        if cur_state["remote"]["status"] == "READY":
            load_remote_actions(url)
            self.action_prep(name)
            self.actions_state.remote_action_loaded(name)
            if unload_existing:
                res = self.unload_action_module(name)
                logger.info(f"Unload action module {name} {res}")
            return True

        return False

    def load_action_module(self, name, unload_existing=False):
        """
        Load an action module
        """
        cur_state = self.actions_state.get_state(name)
        if cur_state is None:
            cur_state = self.actions_state.init_state(name)

        if cur_state["mode"] == "module":
            logger.info(f"{name} already loaded as module.")
            # Check if there is already a local action loaded
            return True

        if name not in action_configs:
            return False

        module = action_configs[name]["module"]
        loaded_module = action_configs[name]["loaded_module"]
        res = load_module_actions(module, loaded_module)
        if not res:
            return False
        self.action_prep(name)
        self.actions_state.module_action_loaded(name, module, loaded_module)
        if unload_existing:
            self.unload_action_remote(name)
        return True

    def unload_action_auto(self, name):
        """
        Unload an action based on how it is currently loaded
        """
        cur_state = self.actions_state.get_state(name)
        if cur_state is None:
            return False, "Action is not loaded."
        if cur_state["mode"] == "module":
            return self.unload_action_module(name)
        elif cur_state["mode"] == "remote":
            return self.unload_action_remote(name)
        return False, f"Unrecognized action loaded status {cur_state['mode']}"

    def unload_action_module(self, name):
        """
        Unload an action module
        """
        cur_state = self.actions_state.get_state(name)
        if cur_state is None:
            return False, "Action is not loaded."

        # if cur_state["mode"] != "module":
        #     return False, "Action is not loaded as module."

        module_name = cur_state["module"]["name"]
        loaded_module = cur_state["module"]["loaded_module"]

        unload_module(module_name)
        unload_module(loaded_module)
        self.actions_state.module_action_unloaded(name)

        return (True, f"Action module {name} unloaded.")

    def unload_action_remote(self, name):
        """
        Unload a remote action
        """
        cur_state = self.actions_state.get_state(name)
        if cur_state is None:
            return False, "Action is not loaded."

        if cur_state["mode"] != "remote":
            return False, "Action is not loaded as remote."

        if cur_state["remote"]["status"] != "READY":
            return False, "Remote action is not ready."

        # Get the list of actions from the action spec of the server
        url = cur_state["remote"]["url"]

        unload_remote_actions(url)
        self.actions_state.remote_action_unloaded(name)

        return (True, f"Remote actions from {url} unloaded.")

    def remote_action_ready_check(self, name, url):
        """
        Check if a remote action is ready by querying the action_spec endpoint
        """
        if url is None:
            return False
        spec_url = url.rstrip("/") + ACTIONS_SPEC_LOC
        headers = {"content-type": "application/json"}
        try:
            res = requests.get(spec_url, headers=headers, timeout=1)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            # Remote service not ready yet
            return False

        return res.status_code == 200

    def set_action_policy(self, policy_name: str, policy_params: dict = {}):
        """
        Set the action optimization policy for JSORC
        """
        # TODO: manage policy switching if there are unresolved actions state
        if policy_name in POLICIES:
            self.policy = policy_name
            self.policy_state[policy_name] = {}
            self.policy_params = policy_params
            return True
        else:
            return f"Policy {policy_name} not found."

    def get_action_policy(self):
        """
        Return the currently active action policy
        """
        return self.policy

    def run(self, jsorc_interval: int):
        """
        The main optimization function.
        This gets invoked by JSROC regularly at a configured interval.
        """
        self.jsorc_interval = jsorc_interval
        if self.policy == "Default":
            # Default policy does not manage action automatically
            return
        elif self.policy == "Evaluation":
            self._actionpolicy_evaluation()
        # if len(self.actions_change) > 0:
        #     self.apply_actions_change()
        elif self.policy == "Auto":
            self._actionpolicy_auto()
        else:
            logger.error(f"Unrecognized policy {self.policy}")

    def _init_evalution_policy(self, policy_state):
        # 999 is just really large memory size so everything can fits in local
        node_mem = self.policy_params.get("node_mem", 999 * 1024)
        jaseci_runtime_mem = self.policy_params.get("jaseci_runtime_mem", 300)
        # Initialize configs to eval
        actions = self.actions_state.get_active_actions()
        # construct list of possible configurations
        all_configs = [{"local_mem": jaseci_runtime_mem}]
        for act in actions:
            new_configs = []
            for con in all_configs:
                for m in ["local", "remote"]:
                    c = copy.deepcopy(con)
                    c[act] = m
                    if m == "local":
                        local_mem_requirement = action_configs[act][
                            "local_mem_requirement"
                        ]
                        c["local_mem"] = c["local_mem"] + local_mem_requirement
                        if c["local_mem"] < (node_mem * NODE_MEM_THRESHOLD):
                            new_configs.append(dict(c))
                        else:
                            logger.info(
                                f"""config dropped for memory constraint: {c},
                                \n\tcurrent node memory: {node_mem}
                                \n\tavailable memory: {(node_mem * NODE_MEM_THRESHOLD)-c['local_mem'] }"""  # noqa: E501
                            )
                    else:
                        new_configs.append(dict(c))
            all_configs = list(new_configs)

        def get_config_distance(config1, config2):
            num_changes = 0
            for key in config1.keys():
                if config1[key] != config2[key]:
                    num_changes += 1
            return num_changes

        # Sort the configurations based on the minimum changes between each config
        sorted_configurations = [all_configs[0]]  # Start with the first configuration

        while len(sorted_configurations) < len(all_configs):
            min_distance = float("inf")
            min_config = None

            for config in all_configs:
                if config not in sorted_configurations:
                    distance = min(
                        get_config_distance(config, sorted_config)
                        for sorted_config in sorted_configurations
                    )
                    if distance < min_distance:
                        min_distance = distance
                        min_config = config

            sorted_configurations.append(min_config)
        logger.info(f"config selected for evaluation: {sorted_configurations}")
        policy_state["remain_configs"] = sorted_configurations

    def _get_walker_latency(self):
        """
        Get the average latency of walkers
        """
        walker_runs = []
        if "walker_run" in self.benchmark["requests"]:
            for walker, times in self.benchmark["requests"]["walker_run"].items():
                if walker == "_default_":
                    continue
                else:
                    walker_runs.extend(times)
            latency = sum(walker_runs) / len(walker_runs)
        else:
            latency = 0.0
        logger.info(f"===walker latency===\nlatency: {latency}")
        return latency

    def _check_phase_change(self, policy_state):
        """
        Check if the current system state has changed
        """
        curr_start_window = len(policy_state["prev_avg_walker_lat"]) - WINDOW_SIZE
        curr_end_window = curr_start_window + WINDOW_SIZE
        prev_start_window = len(policy_state["prev_avg_walker_lat"]) - (WINDOW_SIZE + 1)
        prev_end_window = prev_start_window + WINDOW_SIZE

        lat_change_pct = abs(
            sum(policy_state["prev_avg_walker_lat"][prev_start_window:prev_end_window])
            - sum(
                policy_state["prev_avg_walker_lat"][curr_start_window:curr_end_window]
            )
        )
        if (lat_change_pct > THRESHOLD) or set(policy_state["prev_actions"]) != set(
            list(self.actions_calls.keys())
        ):
            # if walker latency changes too much, kick the evaluation phase
            logger.info(
                f"""===walker latency changes===
                \nlat_change_pct: {lat_change_pct}
                \nprev_actions: {policy_state["prev_actions"]}
                \nactions_calls: {list(self.actions_calls.keys())}
                \nneed to kick in evaluation"""
            )
            return True
        else:
            logger.info(
                f"""===walker latency is not more than previous state===
                \nlat_change_pct: {lat_change_pct}
                \nprev_avg_walker_lat :{policy_state['prev_avg_walker_lat']}"""
            )
            return False

    def _actionpolicy_auto(self):
        """
        A automatic policy that automatically loads and unloads
        actions based on the current workload.
        """
        logger.info("===Auto Policy===")
        policy_state = self.policy_state["Auto"]
        # check if we should go into evaluation phase
        # compare the current interval with the previous interval
        # compare the walker state for each interval and if change go in the eval phase
        if len(policy_state) == 0:
            # Initialize policy tracking state
            policy_state = {
                "phase": "eval",  # current phase of policy: eval|perf
                "cur_config": None,  # current active configuration
                "remain_configs": [],  # remaining config that need to be evaluated
                "past_configs": [],  # configurations already evaluated
                "eval_phase": self.policy_params.get(
                    "eval_phase", 10
                ),  # how long is evaluatin period (in seconds)
                "perf_phase": self.policy_params.get(
                    "perf_phase", 100
                ),  # how long is the performance period (in seconds)
                "cur_phase": 0,  # how long the current period has been running
                "prev_best_config": self.actions_state.get_all_state(),
                "prev_actions": [],
                "action_utilz": {},
                "eval_complete": False,
                "prev_avg_walker_lat": [],
                "call_counter": 0,  # counter for number of calls
            }
        logger.info(f"===Auto Policy=== {policy_state}")
        policy_state["cur_phase"] += self.jsorc_interval
        if policy_state["phase"] == "pref":
            action_utilz = {}
            total_count = 0
            if policy_state["call_counter"] < WINDOW_SIZE:
                # Increment the call counter
                policy_state["call_counter"] += 1
                logger.info(
                    f"Waiting for ({(WINDOW_SIZE+1) - policy_state['call_counter']} more calls before starting the policy state."  # noqa: E501
                )
                policy_state["prev_avg_walker_lat"].append(self._get_walker_latency())
                for action in self.actions_calls.keys():
                    action_utilz[action] = len(self.actions_calls[action])
                    total_count = total_count + len(self.actions_calls[action])
                action_utilz["total_call_count"] = total_count
                logger.info(f"===Auto Policy=== action_utilz: {action_utilz}")

            policy_state["prev_avg_walker_lat"].append(self._get_walker_latency())
            if self._check_phase_change(policy_state):
                # if no enough walker were execueted in this period, keep in perf phase
                logger.info(
                    f"""==in check phase===
                    \npolicy_state: {policy_state}
                    \nprev_avg_walker_lat :{policy_state['prev_avg_walker_lat']}"""
                )
            policy_state["prev_actions"] = list(self.actions_calls.keys())
        elif policy_state["phase"] == "eval":
            if policy_state["cur_config"] is None:
                self._init_evalution_policy(policy_state)
                # This is the start of evaluation period
                policy_state["cur_config"] = policy_state["remain_configs"][0]
                del policy_state["remain_configs"][0]
                policy_state["cur_phase"] = 0
                self.benchmark["active"] = True
                self.benchmark["requests"] = {}
                self.actions_change = self._get_action_change(
                    policy_state["cur_config"]
                )
                if len(self.actions_change) > 0:
                    logger.info(
                        f"===Evaluation Policy=== Switching eval config to {policy_state['cur_config']}"  # noqa: E501
                    )
                    policy_state["phase"] = "eval_switching"
                    self.benchmark["active"] = False
            else:
                if policy_state["cur_phase"] >= policy_state["eval_phase"]:
                    # Get performance
                    if "walker_run" not in self.benchmark["requests"]:
                        # meaning no incoming requests during this period.
                        # stay in this phase
                        logger.info("===Evaluation Policy=== No walkers were executed")
                        self.policy_state["Auto"] = policy_state
                        return
                    walker_runs = []
                    for walker, times in self.benchmark["requests"][
                        "walker_run"
                    ].items():
                        if walker == "_default_":
                            continue
                        else:
                            walker_runs.extend(times)

                    avg_walker_lat = sum(walker_runs) / len(walker_runs)
                    policy_state["cur_config"]["avg_walker_lat"] = avg_walker_lat
                    policy_state["past_configs"].append(policy_state["cur_config"])
                    logger.info(
                        f"""===Evaluation Policy=== Complete evaluation period for:
                            {policy_state['cur_config']} latency: {avg_walker_lat}"""
                    )
                    if len(policy_state["remain_configs"]) == 0:
                        # need to paas the control to Auto phase
                        logger.info("===Evaluation Policy=== Evaluation phase over.")
                        best_config = copy.deepcopy(
                            min(
                                policy_state["past_configs"],
                                key=lambda x: x["avg_walker_lat"],
                            )
                        )
                        self.actions_change = self._get_action_change(best_config)
                        policy_state["phase"] = "pref"
                        policy_state["cur_config"] = None
                        policy_state["past_configs"] = []
                        policy_state["cur_phase"] = 0
                        policy_state["eval_complete"] = True
                        policy_state["prev_best_config"] = best_config
                        self.benchmark["requests"] = {}
                        self.benchmark["active"] = True

                    else:
                        next_config = policy_state["remain_configs"][0]
                        del policy_state["remain_configs"][0]
                        self.actions_change = self._get_action_change(next_config)
                        policy_state["cur_config"] = next_config
                        policy_state["cur_phase"] = 0
                        self.benchmark["requests"] = {}
                        if len(self.actions_change) > 0:
                            logger.info(
                                f"===Evaluation Policy=== Switching eval config to {policy_state['cur_config']}"  # noqa: E501
                            )
                            policy_state["phase"] = "eval_switching"
                            self.benchmark["active"] = False
                        else:
                            policy_state["phase"] = "eval"
                            self.benchmark["active"] = True
                        logger.info(
                            f"===Evaluation Policy=== Switching to next config to evaluate {next_config}"  # noqa: E501
                        )
        elif policy_state["phase"] == "eval_switching":
            # in the middle of switching between configs for evaluation
            if len(self.actions_change) == 0:
                # this means all actions change have been applied, start evaluation phase  # noqa: E501
                logger.info(
                    "===Evaluation Policy=== All actions change have been applied. Start evaluation phase."  # noqa: E501
                )
                policy_state["phase"] = "eval"
                policy_state["cur_phase"] = 0
                self.benchmark["active"] = True
                self.benchmark["requests"] = {}
        self.policy_state["Auto"] = policy_state

    def _actionpolicy_evaluation(self):
        """
        A evaluation based policy.
        JSORC cycle through possible action configurations and
        evaluate request performance and select the one with the best performance.
        Use the post_request_hook from JSORC to track request performance
        """
        logger.info("===Evaluation Policy===")
        policy_state = self.policy_state["Evaluation"]

        if len(policy_state) == 0:
            # Initialize policy tracking state
            policy_state = {
                "phase": "eval",  # current phase of policy: eval|perf
                "cur_config": None,  # current active configuration
                "remain_configs": [],  # remaining configs that need to be evaluated
                "past_configs": [],  # configurations already evaluated
                "eval_phase": self.policy_params.get(
                    "eval_phase", 10
                ),  # how long is evaluatin period (in seconds)
                "perf_phase": self.policy_params.get(
                    "perf_phase", 100
                ),  # how long is the performance period (in seconds)
                "cur_phase": 0,  # how long the current period has been running
                "prev_best_config": self.actions_state.get_all_state(),
            }
        policy_state["cur_phase"] += self.jsorc_interval

        # check if we should go into evaluation phase
        if (
            policy_state["phase"] == "perf"
            and policy_state["cur_phase"] >= policy_state["perf_phase"]
        ):
            # if no enough walker were execueted in this period, keep in perf phase
            if "walker_run" not in self.benchmark["requests"]:
                policy_state["cur_phase"] = 0
            else:
                logger.info("===Evaluation Policy=== Switching to evaluation mode")
                policy_state["phase"] = "eval"
                policy_state["cur_phase"] = 0
                policy_state["cur_config"] = None
                if len(policy_state["remain_configs"]) == 0:
                    self._init_evalution_policy(policy_state)
        if policy_state["phase"] == "eval":
            # In evaluation phase
            if policy_state["cur_config"] is None:
                self._init_evalution_policy(policy_state)

                # This is the start of evaluation period
                policy_state["cur_config"] = policy_state["remain_configs"][0]
                del policy_state["remain_configs"][0]
                policy_state["cur_phase"] = 0
                self.benchmark["active"] = True
                self.benchmark["requests"] = {}
                self.actions_change = self._get_action_change(
                    policy_state["cur_config"]
                )
                if len(self.actions_change) > 0:
                    logger.info(
                        f"===Evaluation Policy=== Switching eval config to {policy_state['cur_config']}"  # noqa: E501
                    )
                    policy_state["phase"] = "eval_switching"
                    self.benchmark["active"] = False
                    self.apply_actions_change()
            else:
                if policy_state["cur_phase"] >= policy_state["eval_phase"]:
                    # The eval phase for the current configuration is complete
                    # Get performance
                    if "walker_run" not in self.benchmark["requests"]:
                        # meaning no incoming requests during this period.
                        # stay in this phase
                        logger.info("===Evaluation Policy=== No walkers were executed")
                        self.policy_state["Evaluation"] = policy_state
                        return

                    walker_runs = []
                    for walker, times in self.benchmark["requests"][
                        "walker_run"
                    ].items():
                        if walker == "_default_":
                            continue
                        else:
                            walker_runs.extend(times)

                    avg_walker_lat = sum(walker_runs) / len(walker_runs)
                    policy_state["cur_config"]["avg_walker_lat"] = avg_walker_lat
                    policy_state["past_configs"].append(policy_state["cur_config"])
                    logger.info(
                        f"""===Evaluation Policy=== Complete evaluation period for:
                         {policy_state['cur_config']} latency: {avg_walker_lat}"""
                    )

                    # check if all configs have been evaluated
                    if len(policy_state["remain_configs"]) == 0:
                        # best config is the one with the fastest walker latency during the evaluation period # noqa: E501
                        logger.info("===Evaluation Policy=== Evaluation phase over.")
                        best_config = copy.deepcopy(
                            min(
                                policy_state["past_configs"],
                                key=lambda x: x["avg_walker_lat"],
                            )
                        )
                        prev_best_config = None
                        for config in policy_state["past_configs"]:
                            if all(
                                [
                                    config[act]
                                    == policy_state["prev_best_config"][act]["mode"]
                                    for act in config.keys()
                                    if act in action_configs.keys()
                                ]
                            ):
                                prev_best_config = config
                        # caluculate the decrease in % for the new configuration
                        lat_decrease_pct = (
                            prev_best_config["avg_walker_lat"]
                            - best_config["avg_walker_lat"]
                        ) / prev_best_config["avg_walker_lat"]
                        # Switch the system to the best config
                        del best_config["avg_walker_lat"]
                        self.actions_change = self._get_action_change(best_config)
                        if len(self.last_eval_configs) == 0:
                            logger.info(
                                f"best_config : {best_config}\nprev_best_config : {policy_state['prev_best_config']}"  # noqa: E501
                            )
                            # ADAPTIVE: if the selected best config is the same config as the previous best one, double the performance period # noqa: E501
                            if (
                                all(
                                    [
                                        best_config[act]
                                        == policy_state["prev_best_config"][act]["mode"]
                                        for act in best_config.keys()
                                        if act in action_configs.keys()
                                    ]
                                )
                                and lat_decrease_pct > THRESHOLD
                            ):

                                policy_state["perf_phase"] *= 2
                                logger.info(
                                    f"===Evaluation Policy=== Best config is the same as previous one. Doubling performance phase to {policy_state['perf_phase']}"  # noqa: E501
                                )
                        else:
                            total_lat = 0
                            if len(self.last_eval_configs) == len(
                                policy_state["past_configs"]
                            ):
                                for prev_config, curr_config in zip(
                                    self.last_eval_configs, policy_state["past_configs"]
                                ):
                                    prev_key = {
                                        key: val
                                        for key, val in prev_config.items()
                                        if key not in ["local_mem", "avg_walker_lat"]
                                    }
                                    curr_key = {
                                        key: val
                                        for key, val in curr_config.items()
                                        if key not in ["local_mem", "avg_walker_lat"]
                                    }
                                    if curr_key == prev_key and all(
                                        value == "local" for value in curr_key.values()
                                    ):
                                        logger.info("Skipping the local configs")
                                        continue
                                    elif curr_key == prev_key:
                                        total_lat += (
                                            prev_config["avg_walker_lat"]
                                            - curr_config["avg_walker_lat"]
                                        ) / prev_config["avg_walker_lat"]
                                if (
                                    abs(total_lat / (len(self.last_eval_configs) - 1))
                                    > THRESHOLD
                                ):
                                    logger.info(
                                        "===Evaluation Policy=== The latency has changed for current config w.r.t previous config, skipping for now"  # noqa: E501
                                    )
                                else:
                                    policy_state["perf_phase"] *= 3
                                    logger.info(
                                        f"===Evaluation Policy=== All current config has same latency as previous one. Doubling performance phase to {policy_state['perf_phase']} "  # noqa: E501
                                    )
                        self.last_eval_configs = copy.deepcopy(
                            policy_state["past_configs"]
                        )
                        policy_state["phase"] = "perf"
                        policy_state["cur_config"] = None
                        policy_state["past_configs"] = []
                        policy_state["cur_phase"] = 0
                        del best_config["local_mem"]
                        temp_config = {}
                        for module, mode in best_config.items():
                            temp_config[module] = {"mode": mode}
                        policy_state["prev_best_config"] = copy.deepcopy(temp_config)
                        self.benchmark["requests"] = {}
                        self.benchmark["active"] = True
                        logger.info(
                            f"===Evaluation Policy=== Evaluation phase over. Selected best config as {best_config}"  # noqa: E501
                        )
                        self.apply_actions_change()
                    else:
                        next_config = policy_state["remain_configs"][0]
                        del policy_state["remain_configs"][0]
                        self.actions_change = self._get_action_change(next_config)
                        policy_state["cur_config"] = next_config
                        policy_state["cur_phase"] = 0
                        self.benchmark["requests"] = {}
                        if len(self.actions_change) > 0:
                            logger.info(
                                f"===Evaluation Policy=== Switching eval config to {policy_state['cur_config']}"  # noqa: E501
                            )
                            policy_state["phase"] = "eval_switching"
                            self.benchmark["active"] = False
                            self.apply_actions_change()

                        logger.info(
                            f"===Evaluation Policy=== Switching to next config to evaluate {next_config}"  # noqa: E501
                        )
        if policy_state["phase"] == "eval_switching":
            # in the middle of switching between configs for evaluation
            if len(self.actions_change) == 0:
                # this means all actions change have been applied, start evaluation phase # noqa: E501
                logger.info(
                    "===Evaluation Policy=== All actions change have been applied. Start evaluation phase."  # noqa: E501
                )
                policy_state["phase"] = "eval"
                policy_state["cur_phase"] = 0
                self.benchmark["active"] = True
                self.benchmark["requests"] = {}
        self.policy_state["Evaluation"] = policy_state

    def _get_action_change(self, new_action_state):
        """
        Given a new desired action state and the current action_state tracking,
        return the change set.
        Prioritize any module to remote switch first to avoid situation
        where linux OOM kill the subprocess first
        """

        def change_to_remote_first(item1, item2):
            if "to_remote" in item1[1] and "to_remote" in item2[1]:
                return 0
            elif "to_remote" in item1[1]:
                return 1
            elif "to_remote" in item2[1]:
                return -1
            else:
                return 0

        change_state = {}
        for name, new_state in new_action_state.items():
            if name not in action_configs.keys():
                continue
            cur = self.actions_state.get_state(name)
            if cur is None:
                cur = self.actions_state.init_state(name)
            if new_state == "local":
                new_state = "module"
            if new_state != cur["mode"]:
                change_str = (
                    f"{cur['mode'] if cur['mode'] is not None else ''}_to_{new_state}"
                )
                change_state[name] = change_str
        sorted_change_state = OrderedDict(
            sorted(change_state.items(), key=cmp_to_key(change_to_remote_first))
        )

        return sorted_change_state

    def apply_actions_change(self):
        """
        Apply any action configuration changes
        """
        actions_change = copy.deepcopy(self.actions_change)
        # For now, to_* and *_to_* are the same logic
        # But this might change down the line
        for name, change_type in actions_change.items():
            logger.info(f"==Actions Optimizer== Changing {name} {change_type}")
            loaded = False
            if change_type in ["to_local", "_to_local", "_to_module", "to_module"]:
                # Switching from no action loaded to local
                loaded = self.load_action_module(name)
            elif change_type == "to_remote":
                loaded = self.load_action_remote(name)
            elif change_type == "local_to_remote" or change_type == "module_to_remote":
                loaded = self.load_action_remote(name, unload_existing=True)
            elif change_type == "remote_to_local" or change_type == "remote_to_module":
                loaded = self.load_action_module(name, unload_existing=False)
            if loaded:
                logger.info(
                    f"==Actions Optimizer== Changing {name} {change_type} success"
                )
                del self.actions_change[name]
                if len(actions_change) > 0 and self.actions_history["active"]:
                    # Summarize action stats during this period and add to previous state # noqa: E501
                    self.summarize_action_calls()
                    self.actions_history["history"].append(
                        {
                            "ts": time.time(),
                            "actions_state": self.actions_state.get_all_state(),
                        }
                    )
            else:
                logger.info(
                    f"==Actions Optimizer== Changing {name} {change_type} failure"
                )

    def summarize_action_calls(self):
        actions_summary = {}
        for action_name, calls in self.actions_calls.items():
            actions_summary[action_name] = sum(calls) / len(calls)
        self.actions_calls.clear()

        if len(self.actions_history["history"]) > 0:
            self.actions_history["history"][-1]["actions_calls"] = actions_summary
