# xiasl.net 互联网档案库爬虫

`wayback_xiasl.py` 从 Internet Archive / Wayback Machine 的 CDX 索引枚举 `xiasl.net` **及所有子域名**，下载有效的原始存档。使用 Python 标准库，无须 pip 安装。运行环境：Python 3.10+，Linux、macOS 或 Termux（使用 `fcntl` 文件锁，不直接支持 Windows）。

## 分层模型与设计目标

流程不再是「capture → 逐个 replay」，而是：

```text
完整 CDX inventory → 本地分析 → canonicalization → logical object
   → digest 分组 → capture 评分 → download_jobs → replay → blob 去重 → 统计
```

数据分层保存，**历史 metadata 一条都不删**：

```text
logical_object → original URL → capture(含 CDX metadata) → blob(物理文件)
```

核心原则：完整保存历史记录，但相同内容不重复下载物理 payload。所谓相同内容由 CDX `digest`（同一对象内）与实际落地文件的 SHA-256（全局）共同判定。

关键收益（真实 inventory 163,885 条 capture 实测）：

| 指标 | 数量 |
| --- | --- |
| raw captures | 163,885 |
| unique original URLs | 98,853 |
| canonical logical objects | 60,337 |
| unique (object, digest) | 139,381 |
| unique CDX digests | 127,489 |
| 旧算法（latest，逐 URL）请求数 | 98,853 |
| **新算法（latest）计划请求数** | **60,337** |
| 减少比例 | **39.0%**（对比逐 capture 则减少 63.2%） |
| `--mode all` 计划请求数 | 139,381（实际去重后 ≈127,489 个唯一 digest） |

## 快速开始

默认保存到手机公共目录 **`/storage/emulated/0/Download/xiasl_archive`**，可在文件管理器的“内部存储 → Download → xiasl_archive”查看，不再保存到 Termux 私有目录。非 Android 默认使用 `~/Downloads/xiasl_archive`。如果提示无存储权限，先运行 `termux-setup-storage` 并授权。

在 Termux 任意目录可运行 `python ~/dsh/wayback_xiasl.py`。下面示例假设已切换到脚本目录；带公共绝对路径的示例适用于 Android：

```bash
# 默认：全部时间范围，每个精确 URL 最新的有效版本，包含图片、CSS 等资源
python wayback_xiasl.py

# 所有有效历史版本；建议单独输出目录，方便管理
python wayback_xiasl.py --mode all --output /storage/emulated/0/Download/xiasl_history

# 只下载网页，限制到 2003–2010 年
python wayback_xiasl.py --html-only --from 2003 --to 2010 --output /storage/emulated/0/Download/xiasl_pages_2003_2010

# 小规模试跑：只扫描两页索引、最多尝试下载五份存档
python wayback_xiasl.py --page-size 100 --max-pages 2 --max-downloads 5 --output /storage/emulated/0/Download/xiasl_trial

# 仅获取索引
python wayback_xiasl.py --index-only

# 以后重新查询新增存档，但不重复下载已校验的文件
python wayback_xiasl.py --refresh-index

python wayback_xiasl.py --help
```

### 分析与分阶段运行

```bash
# 只读分析：输出 analysis.json（不访问 replay）
python wayback_xiasl.py --analyze

# 只做分层与规划，打印 A–E 与请求预算（不访问 replay）
python wayback_xiasl.py --dry-run

# 按资源优先级分阶段下载：先正文，再附件，再辅助资源
python wayback_xiasl.py --skip-index --priority-max 0
python wayback_xiasl.py --skip-index --priority-max 1
python wayback_xiasl.py --skip-index --resource-type thread,article,forum

# 重新规划 job（索引新增、或改了分类规则之后）
python wayback_xiasl.py --skip-index --replan

# 分析已下载 HTML 的链接关系（不产生新请求）
python wayback_xiasl.py --scan-links
```

新增参数：`--analyze`、`--dry-run`、`--replan`、`--priority-max`（0–4）、`--resource-type`、`--scan-links`、`--fallback-candidates`、`--max-delay`、`--no-adaptive`、`--no-verify-blobs`。原有参数（`--domain/--output/--mode/--from/--to/--html-only/--index-only/--skip-index/--min-timestamp/--max-timestamp/--refresh-index/--page-size/--max-pages/--max-downloads/--delay/--timeout/--retries/--max-file-bytes/--user-agent/--verbose`）全部继续可用，`--max-downloads` 现在限制的是 **replay 尝试次数**。

## URL canonicalization 规则（基于真实 URL 结构）

| canonical key | 识别的 URL 形态 | priority |
| --- | --- | --- |
| `thread:<tid>:page:<n>` | `thread-<tid>-<n>-1.html`、`viewthread-<tid>.html`、`viewthread.php?tid=`（含 `/tszrbbs/`、`bbs` 主机） | 0 |
| `forum:<fid>:page:<n>` | `forum-<fid>-<n>.html`、`forumdisplay.php?fid=` | 0 |
| `user:<uid>[:op]:page:<n>` | `space-uid-<uid>.html`、`space.php?uid=`、`home.php?mod=space&uid=` | 2 |
| `article:<id>[:print\|:comment\|:mail]` | `Article/<id>.html`、`shownews_<id>.asp`、`ArticleID=`/`newsid=` | 0 |
| `author:<name>:channel:<id>` | `ShowAuthor.asp?ChannelID=&AuthorName=` | 2 |
| `tag:<name>` | `tag.php?name=` | 0 |
| `attachment:<aid>` / `attachment:<sha1>` | `attachment.php?aid=`、`/attachments/YYYY/MM/...` | 1 |
| `generic:<host><path>?<cleaned query>` | 其他一切（**不丢弃**） | 按 mimetype |

保守原则（宁可漏合并，不可误合并）：

- 主机名只做 `www.` 前缀与默认端口归一化；`bbs`、`novel`、`t` 等子域各自独立，只有 `bbs.xiasl.net` 的 `/tszrbbs/` 路径因同为 Discuz 安装而与 `www` 归入同一 `thread:` 对象（有路径证据，但仍是本工具中**最保守之外的一处推断**）。
- 分页只在页面语义明确时参与 key（`thread-…-2-…`、`page=2`），**不同页永不合并**。
- 噪声参数（`extra`、`highlight`、`sid`、`from`、`rpid`、`rand`、`orderby`、`ordertype`、`filter`、`formhash`、`inajax` 等）在生成 key 时丢弃；查询值保持原样不做解码，因为本站 URL 里存的是 GBK 字节。
- 无法识别的 URL 一律保存为 `generic:`，绝不丢弃。

## 失败分类与重试

`error` 不再是笼统状态，按类别区分：

```text
rate_limited / temporary_wayback_error / network_error / replay_missing
wayback_runtime_error          → 可重试，带 attempts、last_attempt、next_retry_at（指数退避，上限 6 小时）
historical_failure / invalid_capture / permanent_error → 永久，不再重试
local_corruption               → 本地文件缺失或校验失败，重新下载
```

永久失败不会每次启动重新请求；临时失败到期后才重新排队。

## 自适应调速

单 worker、不增加并发。维护最近 50 次请求的成功/限流/网络错误与平均延迟：

- 限流或 5xx 比例超过 10%，或网络拒绝超过 25% → delay ×1.5（上限 `--max-delay`，默认至少 30 秒）
- 连续 30 次以上成功后 → delay ×0.9（不低于 `--delay`）
- 始终尊重 `Retry-After`（秒数或 HTTP 日期）

运行中定期打印：captures / logical objects / jobs / done / pending / blobs / bytes / 当前 priority / req-min / delay / 429 / 503 / 临时失败 / 永久失败 / ETA（按剩余 job ÷ 最近实际吞吐计算）。

默认先收集完整索引，再下载正文。**`--max-downloads` 只限制正文下载尝试数，不限制索引扫描**；首次测试可搭配 `--max-pages`。部分索引上的“最新”仅指已收集到的候选版本；完整索引扫描后才代表整个指定范围。索引以 URL/时间顺序分页，不保证首页最先出现。

## 过滤规则与边界

1. **历史响应状态**：CDX 查询使用 `filter=statuscode:200`，本地再次检查。因此历史 4xx、5xx、重定向、206 和状态未知记录均不作为正文候选。这里采用严格的“完整 HTTP 200 正文”标准；未知状态不等于抓取失败，只是无法确认而不纳入。历史 3xx 可能是正常跳转，本工具不保存跳转壳页面。
2. **当前回放**：通过 `https://web.archive.org/web/<timestamp>id_/<original>` 下载，不带工具栏、不改写正文。要求合法的 `Memento-Datetime` 作为存档证据；缺失时记为可重试错误，防止把 HTTP 200 的临时封禁页当作正文。禁止跳转到真实网站，避免误抓现在的网页。若 Wayback 替换时间戳或跳到不同原网址，不保存该结果。
3. **档案库错误页**：检查回放运行错误头，以及同时具有 Wayback 标题、错误模板标记和特定错误提示的 HTML。它们记为 `error`，不是当年失败的证据。
4. **伪 200 失败正文**：空正文会被过滤；短小的标准 502/503/504/Proxy Error 标题页，有 `Memento-Datetime` 存档证明时记为 `filtered`，没有时记为当前回放 `error`。原始状态头若显示非 200 也会过滤。这是保守启发式，并不能识别所有站点自定义“软错误页”。
5. **不会只因页面出现 404、robots.txt 或 Wayback Machine 等普通文字就过滤**，也不把二进制文件当 HTML 扫描。

`filtered`：已识别的历史失败/不适用正文，下次不再尝试。`error`：当前下载失败、不可访问、回放替换、尺寸超限等，下次可重试。两者都不会保存为成功正文。`pending`：尚未尝试。

默认 `latest` 模式按每个**精确 original URL**从新到旧尝试，失败后回退更早存档。HTTP/HTTPS、`www`、端口和查询参数不同的 URL 不会合并。`all` 模式保存每个候选历史版本，不按摘要去重。

**限制：**200 页面可能是停放域名、广告或自定义错误页，不一定仍是原站内容；无法在无误判的情况下全自动判断。CDX 未收录/被屏蔽的页面无法恢复。默认按 URL 选择版本，得到的文件可能跨多个年份，不是同一时间的网站快照。

## 输出

默认目录 `/storage/emulated/0/Download/xiasl_archive/`（Android）：

```text
xiasl_archive/
├── state.sqlite3       # 分层状态：captures / urls / logical_objects / object_digests
│                       #            download_jobs / job_candidates / blobs / links
├── analysis.json       # --analyze 的完整统计
├── manifest.jsonl      # 每个已保存 capture 的可追溯记录
├── failures.jsonl      # 过滤/失败记录，含 failure_class、retry_count、next_retry_at
├── blobs/ab/<sha256>.<ext>   # 去重后的物理文件（新下载写这里）
└── files/              # 旧版本布局，已注册为 blob 并继续复用，不重新下载
```

`manifest.jsonl` 每行包含：`original_url, canonical_key, object_type, object_id, page, timestamp, cdx_digest, cdx_length, mimetype, resource_type, priority, replay_url, blob_sha256, blob_path, bytes, state`，足以日后重建站点、检查缺页、检查缺附件、检查时间覆盖。


已将原 `~/dsh/xiasl_trial` 的 5 份正文、200 条索引及断点迁入此目录，原路径保留为符号链接；默认命令会继续这一批进度。原 `~/dsh/xiasl_smoke` 的 1 份正文及 4 条索引单独迁入 `/storage/emulated/0/Download/xiasl_smoke/`，原路径同样保留符号链接。迁移逐文件校验 SHA256，并检查数据库完整性。

显式 `--output` 仍优先于默认路径；相对路径仍相对于当前工作目录，请优先使用公共存储的绝对路径。

```text
xiasl_archive/
  state.sqlite3      # 索引、分页 checkpoint、下载/过滤状态
  manifest.jsonl     # 成功正文清单
  failures.jsonl     # 被过滤及当前下载失败清单，附原因
  files/
    <hash 前两位>/<完整 URL 的 SHA256>/<时间戳>.<扩展名>
```

清单每行是一个 JSON 对象，包含 `original`、`timestamp`、`mimetype`、`statuscode`、`digest`、`state`、`path`、`sha256`、`bytes`、`replay_url`、`error` 等。`path` 相对于输出目录；CDX 的 `length` 是档案记录长度，不应当用它验证正文长度。

URL 使用哈希路径，防止路径穿越、查询参数冲突、过长文件名。正文保持原始字节，不自动转码，GB2312/GBK 等旧网页不会因保存为 UTF-8 而损坏。扩展名由 CDX MIME 推断；未知 MIME（含部分 revisit）为 `.bin`。

这些文件是**原始存档集合，不是可离线导航的网站镜像**：链接仍为原站路径，不改写本地链接。使用清单查找 URL 对应文件；如要离线镜像，需要另行重建路径及改写链接。历史网页可能含脚本或表单，查看时不要输入个人信息。

## 断点续爬、限速与刷新

- Ctrl+C 后重新运行相同命令即可；每页索引和每份下载分别提交 SQLite 事务，分页进度不会先于记录提交。
- 默认每次请求间隔至少 1.5 秒，单并发；网络错误、当前 429/部分 5xx 最多重试 4 次，指数退避并考虑 `Retry-After`。HTTP 200 但内容验证失败则记录错误，下一次运行再尝试，而不是使用本次网络重试预算。不要为了提速对档案库高并发施压。
- 限制每次尝试数时优先处理尚未尝试的 URL；同一 URL 有未尝试版本时先尝试这些版本，再于后续运行重试以前失败的版本，避免一个不可用页面耗尽每次配额。
- 默认超时 45 秒，单文件最多 50 MiB。可用 `--timeout`、`--retries`、`--delay`、`--max-file-bytes` 调整。
- 已下载文件会校验尺寸和 SHA256；缺失或损坏时重新下载。临时文件写完后原子替换，同一目录禁止两个爬虫同时运行。Android 公共存储不支持 `flock` 时，协调锁自动存到 Termux 私有的 `~/.cache/xiasl_wayback/locks/`；正文、清单和 SQLite 断点仍全部在公共目录。
- 同一输出目录绑定域名、日期范围和 `--html-only`。要改变这些范围请使用新目录；`--refresh-index` 不改变范围，也不清除已有过滤状态。
- `latest` 清单只输出每个 URL 已保存的最新版本；刷新后旧文件可能保留在磁盘上，以免破坏已有存档。`all` 清单列出所有已保存版本。
- 单份回放失败不会终止整个任务，须看日志/`failures.jsonl`。退出码 0 表示这次任务正常结束（也可能达到人为限制），**不保证所有候选均成功**；索引或状态存储异常返回 1，Ctrl+C 返回 130。

## 在 dev-sg 服务器上下载

索引已在手机端扫完，正文下载由 dev-sg 承担。

```bash
# 从手机部署代码；--with-index 会同时合并完整索引（保留服务器已有的下载状态）
bash ~/dsh/deploy_to_dev_sg.sh --with-index

# 服务器上启动/查看任务
ssh dev-sg
tmux attach -t xiasl          # 查看运行中的任务（Ctrl+B 然后 D 退出查看）
tail -f ~/xiasl-wayback/logs/run4.log
```

服务器命令使用 `--skip-index`（索引已完整，不必重扫）和显式 `--output ~/xiasl-wayback`，因为那台机器上 `/storage/emulated/0` 也存在，默认路径会落到 `~/Downloads/xiasl_archive`。

当前进度（随时用下列命令查询）：

```bash
ssh dev-sg 'cd ~/xiasl-wayback && python3 -c "
import sqlite3
db = sqlite3.connect(\"file:state.sqlite3?mode=ro\", uri=True)
print(db.execute(\"SELECT state, COUNT(*) FROM captures GROUP BY state\").fetchall())
"'
```

**三台机器并行**：档案库对单一 IP 有速率上限（请求过快会返回 `Connection refused`，放慢到 6 秒间隔即完全消失），因此按时间片分工。时间片在**执行时**生效（`pending_jobs` 用「该 job 是否有落在窗口内的候选」判定），所以无需重新规划即可调整分工。

| 机器 | 负责时间片 | 状态目录 | 日志 | 实测速率 |
| --- | --- | --- | --- | --- |
| mc-hk | 2003–2009 | `/root/xiasl-wayback` | `logs/mchk3.log` | ~13 次/分钟 |
| dev-sg | 2010-06-16 及以后 | `/home/dev/xiasl-wayback` | `logs/run13.log` | ~17 次/分钟 |
| **手机（Termux）** | 2010-01-01 – 2010-06-15 | `/storage/emulated/0/Download/xiasl_phone` | `xiasl_phone/run.log` | ~6 次/分钟 |

手机作为第三台 worker 的启动方式（新文件单独放在 `xiasl_phone`，不与原 `xiasl_archive` 混放）：

```bash
cd ~/dsh && python wayback_xiasl.py --output /storage/emulated/0/Download/xiasl_phone \
  --skip-index --min-timestamp 20100101000000 --max-timestamp 20100615235959 \
  --delay 1.5 --timeout 40 --retries 4
```

手机端瓶颈是 Android 共享存储的写入/fsync，速率约 6 次/分钟；dev-sg 启动时若不想每次重启都对上万 blob 做 SHA-256 校验，可加 `--no-verify-blobs`（默认仍逐个校验）。

启动方式（均已使用 `--skip-index` + 显式 `--output`）：

```bash
bash ~/dsh/deploy_to_dev_sg.sh --with-index   # 手机 → dev-sg（合并完整索引）
bash ~/dsh/deploy_to_mc_hk.sh                 # 手机 → mc-hk（发送 2003–2009 时间片）
```

合并回手机（先停止对应机器的爬虫，`merge_archive.py` 校验 SHA256 后复制，只搬运成功下载的行）：

```bash
cd ~/dsh
python merge_archive.py /path/to/server_archive /storage/emulated/0/Download/xiasl_archive
```

**磁盘限制**：dev-sg 磁盘偏紧（清空间后约 3.5 GB 可用），按每份平均 22 KB 估算，一轮 80000 份约占 1.7 GB。`--max-downloads` 达到后任务正常退出，再次执行同一命令即继续；磁盘写满时任务以非零码退出且已下载内容全部保留，清出空间后续跑即可。若某轮因空间不足提前结束，把 `--min-timestamp` 设为“最后成功下载的时间戳+1”可跳过已完成部分，继续向后推进。

**速率限制**：服务器曾因请求过快被档案库短暂拒绝连接（`Connection refused`）。已验证 0.8–1.5 秒间隔可稳定请求。若日志再次频繁出现该错误，请把任务停下，把 `--delay` 提高到 3 秒以上再继续。

**合并语义**：`merge_remote_index.py` 用 `INSERT OR IGNORE` 合并，服务器已有的行（含 `downloaded`）永远优先，因此重复部署安全，不会让已下载记录退回待下载。合并时会跳过 `scope` 之外只同步进度字段。

## 自动交接（handoff）

手机与 mc-hk 会比 dev-sg 先跑完。`handoff.py` 每 2 分钟检查一次：某台 worker 的当前时间片跑完后，就把 dev-sg 剩余 job 中**最新的那一块**（按天分块，粒度足够贴合配额）交给它，并立即把 dev-sg 的时间窗收窄到该块之前，避免重复下载。

配额按「三台同时收工」计算：`quota_i = 剩余 × r_i / Σr`（速率取实测值：dev-sg 17.3、mc-hk 9.7、手机 8.6 次/分钟）。真实数据模拟结果：mc-hk 5,270 个（约 9.1 h）、手机 4,677 个（约 9.1 h）、dev-sg 保留 9,351 个（约 9.0 h），相比 dev-sg 单独跑 18.6 h 缩短一半。

```bash
python ~/dsh/handoff.py             # dry-run：只报告将要做什么
python ~/dsh/handoff.py --execute   # 真正执行，持续监视
python ~/dsh/handoff_simulate.py    # 用 dev-sg 当前真实数据模拟一次分配
```

状态记录在 `~/dsh/handoff_state.json`；日志在 `xiasl_phone/handoff.log`，被交接的 worker 各自写到 `run_handoff.log` / `logs/handoff.log`。mc-hk 的第二个存档目录是 `~/xiasl-wayback-b`（预置了完整索引，首次启动时按窗口自动规划）。

## 静态站构建（把分立文件整理成一个整站）

```bash
# 1) 合并四个来源（按 sha256 校验，只搬运成功下载的行）
python merge_archive.py /home/dev/xiasl-wayback      /storage/emulated/0/Download/xiasl_archive
python merge_archive.py /root/xiasl-wayback-b        /storage/emulated/0/Download/xiasl_archive
python merge_archive.py /root/xiasl-wayback-c        /storage/emulated/0/Download/xiasl_archive
python merge_archive.py /storage/emulated/0/Download/xiasl_phone /storage/emulated/0/Download/xiasl_archive

# 2) 对账（把并进来的内容与 job 表同步）
python wayback_xiasl.py --output /storage/emulated/0/Download/xiasl_archive --skip-index --dry-run --no-verify-blobs

# 3) 生成静态站
python build_static_site.py --archive /storage/emulated/0/Download/xiasl_archive \
       --out /storage/emulated/0/Download/xiasl_site

# 4) 校验链接
python verify_site.py --site /storage/emulated/0/Download/xiasl_site \
       --archive /storage/emulated/0/Download/xiasl_archive
```

采用的默认策略（详见 `static_site_plan.md`）：语义化路径（`/t/<tid>/<页>/`）、每个逻辑对象取最新版本、保留 CSS/JS/图片、外链保留并加 `data-external="1"`、每页注入溯源脚注。

产物：`xiasl_site/`（`index.html` 首页、`inventory/` 可搜索清单、`search/index.json`、`missing/` 缺页说明页、`404.html`、`build-report.json`）。

本地查看（在手机上起一个静态服务器，然后浏览器打开）：

```bash
cd /storage/emulated/0/Download/xiasl_site && python -m http.server 8099
# 浏览器访问 http://127.0.0.1:8099/
```

注意：必须用 HTTP 服务器访问，直接双击 `index.html`（file:// 协议）时站内绝对路径 `/t/...` 无法解析。

## 成品（2026-10-09）

| 产物 | 位置 | 规模 |
| --- | --- | --- |
| 主存档（合并四来源） | `/storage/emulated/0/Download/xiasl_archive` | 71,409 份 capture / 59,346 个对象已完成 |
| 静态站 | `/storage/emulated/0/Download/xiasl_site` | 50,838 页 + 4,444 资源 / 1.89 GB |
| 站点打包 | `/storage/emulated/0/Download/xiasl_site.zip` | 452 MB（55,287 个文件） |

- 完成度：**59,346 / 60,337 = 98.4%**；其余 991 个对象档案库里没有可用内容（历史错误页、回放 404、104 个编码损坏的 URL）
- 站内链接解析率：**99.995%**（2,271,270 / 2,271,373）
- 版块导航：从归档页面里统计出 **21 个版块 / 5,313 个帖子链接**（`/forums/`）
- 未归档的链接（182 万条）统一指向 `missing/` 说明页，不伪造内容

查看方式：

```bash
cd /storage/emulated/0/Download/xiasl_site && python -m http.server 8099
# 浏览器打开 http://127.0.0.1:8099/
```

## 数据质量提醒

索引里存在少量**档案库记录的网址本身就是坏的**：部分中文作者名被以错误编码写入 CDX（例如 `AuthorName=记者+刘荣　实习生+周微` 变成泰文/老挝文字符），这些候选无法正确回放，会进入 `failures.jsonl`，属于档案库自身的数据问题，不是下载失败。

## 测试

```bash
python -m unittest -v test_wayback_xiasl.py
```

53 项自动化测试**完全不访问网络**（`--analyze`/`--dry-run` 的用例会断言绝不触碰 replay），覆盖：

- CDX JSON 与文本两种格式、分页 resumeKey、断点、重复 token、不完整分页框架
- 旧 schema 自动迁移（保留 downloaded/filtered/error 状态、replay_url、已完成索引）
- thread URL 各形态归并、分页区分、不同 thread 不误合并、attachment/forum/user/article 识别、generic 兜底、噪声参数剔除
- digest 分组、logical-object/digest 分组、preferred/fallback 选择、priority 与 resource-type 过滤
- 同 digest 只下载一次、已有文件复用不 replay、本地损坏触发重下、失败分类与重试调度
- 链接关系记录（站内 vs 未解决）、缺页线索只在有证据时生成
- 原有网络层保障：回放替换拒绝、原站跳转阻止、档案库错误模板、合法文章误判防护、GBK 原样保存、安全文件名、截断下载与尺寸限制

本次还做了有限联网验证：`xiasl_smoke/manifest.jsonl` 记录了成功保存的 2003-12-22 首页，正文 31,105 字节。随后复跑验证索引从 checkpoint 继续、已保存正文不重复下载。该目录只是试跑结果，**不是完整网站存档**。

接口依据：[Internet Archive CDX Server API 文档](https://github.com/internetarchive/wayback/blob/master/wayback-cdx-server/README.md)。仅处理公开可访问的存档，不绕过访问限制。
