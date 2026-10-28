# xiasl.net 历史浏览站 — GitHub Pages 发布包

默认进入 2017 年原版首页；首页年份保留在 URL 中。历史正文、图片和来源信息来自已有归档。登录、发帖、支付等原站后端功能不提供服务。

浏览器 Service Worker 根据 URL 和历史年份选择内容；gzip 文件按需解压，不需要 Python 服务。全文搜索保留来源筛选。“本地仅 CC”不能证明其他档案馆没有保存。

需要 HTTPS（本地预览允许 localhost）、JavaScript、Service Worker 和 DecompressionStream。首次打开深链接可能先经过 GitHub Pages 的 404 引导页，随后由本地回放路由接管。原始缺档仍显示缺失，不回源加载今日广告或旧域名内容。

站点路径配置为 `/xiasl-archive/`。源码 `export.py` 需在已有档案服务器上运行。发布目录只含生成的公开静态内容，不包含 SQLite、SSH 设置、凭据或服务器日志。`build-report.json` 记录发布范围和失败项；完整性验收通过后才发布。
