#!/usr/bin/env python3

import argparse
import os
from pathlib import Path
import shutil
import sys
from typing import Dict, List


TARGETS = [
    {"ip": "10.66.12.21", "name": "master-a", "cluster": "orders-prod", "status": "online"},
    {"ip": "10.66.12.22", "name": "master-b", "cluster": "payments-prod", "status": "online"},
]

USERS = [
    ("1", "ruby"),
    ("2", "root"),
    ("3", "app"),
    ("4", "readonly"),
]


def main():
    parser = argparse.ArgumentParser(description="Interactive CMSM simulator for ssh-mcp testing.")
    parser.add_argument("--list-targets", action="store_true", help="Print built-in targets and exit.")
    args = parser.parse_args()

    if args.list_targets:
        for index, target in enumerate(TARGETS):
            print(f"{index} {target['ip']} {target['name']} {target['cluster']} {target['status']}")
        return 0

    lab_home = get_lab_home()
    ensure_layout(lab_home)
    print_banner()
    target = choose_target()
    username = choose_user(target)
    launch_master_shell(lab_home, target, username)
    return 0


def get_lab_home():
    raw_home = os.environ.get("SSH_MCP_LAB_HOME")
    if raw_home:
        return Path(raw_home).expanduser().resolve()
    return Path(__file__).resolve().parent


def ensure_layout(lab_home):
    (lab_home / "runtime" / "master-home").mkdir(parents=True, exist_ok=True)
    (lab_home / "runtime" / "pods").mkdir(parents=True, exist_ok=True)


def print_banner():
    print()
    print("==============================================")
    print(" CMSM Simulator")
    print("==============================================")
    print("This test menu mimics: CMSM -> master -> pod.")
    print("Type q at any CMSM prompt to exit.")
    print()


def choose_target():
    while True:
        answer = prompt("Target IP: ").strip()
        if answer.lower() in {"q", "quit", "exit"}:
            raise SystemExit(0)
        if not answer:
            print("Known targets:")
            for index, target in enumerate(TARGETS):
                print_result(index, target)
            continue

        for index, target in enumerate(TARGETS):
            if answer in {target["ip"], target["name"], str(index)}:
                print("Search result:")
                print_result(0, target)
                return target

        target = {
            "ip": answer,
            "name": "master-" + answer.replace(".", "-").replace(":", "-"),
            "cluster": "custom-lab",
            "status": "online",
        }
        print("Search result:")
        print_result(0, target)
        return target


def choose_user(target):
    print()
    print(f"Host: {target['name']} ({target['ip']})")
    print("Select login user:")
    for number, username in USERS:
        print(f"{number}) {username}")

    choices = {number: username for number, username in USERS}
    names = {username: username for _, username in USERS}
    while True:
        answer = prompt("User number: ").strip()
        if answer.lower() in {"q", "quit", "exit"}:
            raise SystemExit(0)
        if answer in choices:
            return choices[answer]
        if answer in names:
            return names[answer]
        print("Invalid user. Choose 1, 2, 3, or 4.")


def launch_master_shell(lab_home, target, username):
    master_home = lab_home / "runtime" / "master-home" / username
    master_home.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["SSH_MCP_LAB_HOME"] = str(lab_home)
    env["SSH_MCP_LAB_INSIDE"] = "1"
    env["SSH_MCP_LAB_TARGET_IP"] = target["ip"]
    env["SSH_MCP_LAB_TARGET_HOST"] = target["name"]
    env["SSH_MCP_LAB_CLUSTER"] = target["cluster"]
    env["SSH_MCP_LAB_REMOTE_USER"] = username
    env["PATH"] = str(lab_home / "bin") + os.pathsep + env.get("PATH", "")

    prompt_char = "#" if username == "root" else "$"
    env["PS1"] = f"[{username}@{target['name']} \\W]{prompt_char} "

    shell = choose_shell()
    print()
    print(f"Logged in to {target['name']} as {username}.")
    print("Try: kubectl get pods -n orders")
    print("Try: kubectl exec -it orders-api-7d9d65f8b4-2plxq -n orders -- /bin/bash")
    print()

    os.chdir(master_home)
    os.execvpe(shell[0], shell, env)


def choose_shell():
    bash = shutil.which("bash")
    if bash:
        return [bash, "--noprofile", "--norc", "-i"]
    sh = shutil.which("sh") or "/bin/sh"
    return [sh, "-i"]


def print_result(index, target):
    print(f"{index}  {target['ip']}  {target['name']}  {target['cluster']}  {target['status']}")


def prompt(text):
    sys.stdout.write(text)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if line == "":
        raise SystemExit(0)
    return line


if __name__ == "__main__":
    raise SystemExit(main())
