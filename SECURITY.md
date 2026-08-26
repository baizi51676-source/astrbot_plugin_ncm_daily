# Security Policy / 安全政策

## 支持的版本 / Supported Versions

| 版本 | 支持状态 |
|---|---|
| v1.1.0（latest） | ✅ 积极维护 |
| v1.0.0 | ❌ 不再支持（仅作历史参考） |

## 报告漏洞 / Reporting a Vulnerability

发现安全漏洞或隐私风险时，请按以下方式报告：

1. **首选**：GitHub 私有漏洞报告（仓库页面 → `Security` → `Report a vulnerability`）
2. 备选：在 [Issues](https://github.com/baizi51676-source/astrbot_plugin_ncm_daily/issues) 提交，**请勿在正文中包含任何 Cookie / Token / 账号信息**
3. 报告请包含：
   - AstrBot 版本、运行平台（Windows / Linux）
   - 复现步骤与现象
   - 日志中 `[ncm]` 开头的相关行
   - 是否涉及凭据泄露（如 MUSIC_U、GitHub Token）

我们会在 **7 天内**确认并评估，修复后发布补丁版本。

## 安全注意事项 / Security Notes

### 🔑 MUSIC_U Cookie（网易云账号登录态）

`MUSIC_U` 相当于你的**网易云账号登录态**，拥有它可以读取你的日推、歌单等个人数据。

- **请勿分享给任何人**（群聊、公开仓库、截图、日志、聊天记录都不行）
- 有效期通常**几周到几个月**（取决于登录设备与使用频率），失效后日推/歌单获取失败，**重新登录浏览器复制一份即可**，无需重装插件
- 若曾在公开渠道泄露（如发过群聊/截图），请**立即重新登录网易云**使旧登录态作废
- 插件仅在本地用该值请求网易云官方接口，**不存储、不上传、不落日志**

### 🛡️ 权限控制

- `admin_only`（**默认开启**）：`我的歌单` / 歌单详情 / `日推` 仅管理员可用；**不建议关闭**，否则歌单与听歌偏好可能泄露给群内其他人
- `point_song_allowlist`：点歌白名单，**留空 = 所有人可点歌**；管理员始终可点歌
- 交互状态按 `会话:发送者` 隔离，**只有发起者本人**可操作选择流程

### 🔐 凭据管理（开发者/部署者）

- **不要**在对话、日志、配置截图、公开仓库中提交 GitHub Token、Cookie 等敏感信息
- 若 GitHub Personal Access Token 曾在对话或文件中明文出现，建议在 GitHub Settings → Developer settings → 撤销该 Token 并重新生成
- 插件配置中的 `music_u_cookie` 属于敏感数据，请注意 AstrBot 数据目录的访问权限

### 📦 依赖安全

- 插件**零第三方依赖**（纯 Python 标准库实现），供应链攻击面极小
- 所有网络请求仅指向网易云官方接口（`music.163.com`），无第三方上报

## 已知限制 / Known Limitations

- 音乐卡片仅支持 NapCat / OneBot v11（aiocqhttp）平台，其他平台自动降级为网易云链接
- 网易云接口存在风控，请控制调用频率，避免账号被临时限制
- 本插件为**只读**操作，不会修改你的歌单、账号或隐私设置

## License

[MIT](LICENSE)
