# CMSM Simulator Lab

这个目录提供一个可部署到 Linux ECS / 虚拟机上的测试替身，用来模拟公司内的登录链路：

```text
SSH 登录测试机
  -> CMSM 菜单
  -> 输入目标 IP
  -> 选择登录用户
  -> fake master shell
  -> kubectl get pods / kubectl exec
  -> fake pod shell
  -> find / grep / tail 日志
```

它不是 Kubernetes，也不需要真实集群。`kubectl` 是一个小脚本，会创建可搜索的 pod 日志目录，并在 `kubectl exec` 时进入一个真实 bash shell，因此 MCP 的 `search_logs` 仍然可以跑真实 `find + grep` 命令。

## 部署到 ECS

把 `lab/cmsm_simulator` 目录上传到 ECS 后执行：

```bash
cd cmsm_simulator
bash install.sh --prefix "$HOME/ssh-mcp-lab" --auto-start
```

`--auto-start` 会在当前用户的 `~/.bashrc` 加一个受控代码块：只要通过 SSH 打开交互式 shell，就自动进入 CMSM 模拟器。

不想改 `.bashrc` 时，可以只安装：

```bash
bash install.sh --prefix "$HOME/ssh-mcp-lab"
```

然后 SSH 登录后手动运行：

```bash
python3 "$HOME/ssh-mcp-lab/cmsm.py"
```

## 手工测试流程

SSH 登录测试机后，你会看到：

```text
CMSM Simulator
Target IP:
```

输入任意 IP 都可以。内置的推荐目标：

```text
10.66.12.21
10.66.12.22
```

然后选择用户，`2` 是 `root`：

```text
1) ruby
2) root
3) app
4) readonly
```

进入 master 后可以执行：

```bash
kubectl get ns
kubectl get pods -n orders
kubectl exec -it orders-api-7d9d65f8b4-2plxq -n orders -- /bin/bash
```

进入 pod 后可以测试日志搜索：

```bash
find . -type f -name "*.log" -print0 | xargs -0 -r grep -n -i -- "timeout"
grep -n -i -- "payment failed" ./var/log/app/app.log
tail -n 20 ./var/log/app/app.log
```

输入 `exit` 会从 pod 回到 master，再输入 `exit` 会断开模拟 master shell。

## MCP 测试建议

如果启用了 `--auto-start`，MCP 打开 SSH session 后直接按菜单交互：

```text
open_session(profile="lab")
send_text(session_id, "10.66.12.21", wait_for="Select login user")
send_text(session_id, "2", wait_for="root@master")
execute_command(session_id, "kubectl get pods -n orders")
execute_command(session_id, "kubectl exec -it orders-api-7d9d65f8b4-2plxq -n orders -- /bin/bash", wait_for="pod:orders")
search_logs(session_id, pattern="timeout", path=".", include="*.log")
```

如果没有启用自动启动，先让 MCP 手动启动模拟器：

```text
execute_command(session_id, "python3 ~/ssh-mcp-lab/cmsm.py", wait_for="Target IP")
```

