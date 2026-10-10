# 项目约定（给 AI 助手的工作说明）

## Git 推送

本机有时**不会开 VPN**。此时：

- git 全局配置的代理 `http://127.0.0.1:7890` 不可用；
- 系统级 `C:/Program Files/Git/etc/gitconfig` 里 `http.sslBackend=openssl` 这版 Git 不支持。

**没开代理时，用命令行临时绕过代理上传（不要修改全局/系统 git 配置）**：

```powershell
# 先探测一个可用的 GitHub IP（返回 200 就能用）
curl.exe -sS --max-time 8 --resolve github.com:443:<IP> -o NUL -w "%{http_code}`n" https://github.com

git -c http.proxy= -c http.sslBackend=schannel `
    -c http.curloptResolve=github.com:443:<IP> push origin master
```

曾可用 IP：`20.27.177.113`、`140.82.113.4`（`20.205.243.166` 在本网段不通）。IP 会变，探测后再用。
网络确实不通时，重试不超过 3 次，然后如实报告"提交在本地、尚未推送"。

## 部署到阿里云

见 `docs/aliyun-ops.md`。要点：服务器 `/root/project/PM` **不是 git 仓库**，改完要 scp 传文件 + 重启
`private-pm.service`；不要覆盖 `instance/` 与 `uploads/`；动之前先备份。

## 测试

用 `F:\Anaconda3\envs\aliyun\python.exe -m pytest tests -q`（基础环境 `F:\Anaconda3\python.exe`
的 jinja2/markupsafe 不匹配，跑不起来）。
