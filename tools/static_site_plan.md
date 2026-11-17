# xiasl.net 静态站整合计划（已按默认选项实施）

目标：把现在**分立的一堆 blob 文件**变成**一个可离线浏览、链接互通的静态网站**，并且每一个页面都能追溯回「原始 URL + 抓取时间 + CDX digest + 本地 blob」。

实施状态：按默认选项（语义化路径、最新版本、纳入 CSS/JS、外链保留并标记）已实现于 `build_static_site.py`，校验器 `verify_site.py`。

---

## 1. 输入与现状

| 输入 | 内容 |
| --- | --- |
| `state.sqlite3` | `captures`（完整 CDX 历史）、`urls`（URL→canonical）、`logical_objects`、`object_digests`、`blobs`、`links`、`unresolved_links`、`suspected_gaps` |
| `blobs/<xx>/<sha256>.<ext>` | 去重后的物理正文（新下载） |
| `files/<xx>/<sha256>/<ts>.<ext>` | 旧布局，已注册为 blob，等价对待 |
| `manifest.jsonl` | 每个已保存 capture 的可追溯清单 |
| 四个来源目录 | `xiasl_archive`（手机原库）、`xiasl_phone`、dev-sg `xiasl-wayback`、mc-hk `xiasl-wayback-b`/`-c`，需先按 `merge_archive.py` 合并 |

关键点：**同一个 URL 可能有多个历史版本（不同 digest）**，`latest` 模式只下载每个逻辑对象的一份内容。静态站默认用「每个逻辑对象的最新版本」，但归档里保留了全部版本元数据。

## 2. 输出结构

```
site/
├── index.html                     # 首页（最新归档的站点首页）
├── t/<tid>/<page>/index.html      # 帖子页（canonical：thread:<tid>:page:<n>）
├── f/<fid>/<page>/index.html      # 版块列表页（forum:<fid>:page:<n>）
├── u/<uid>/index.html             # 用户页（user:<uid>[:op]）
├── a/<id>/index.html              # 旧 ASP 文章页（article:<id>，含 :print/:comment 变体）
├── asset/<path>                   # 图片/CSS/JS/附件，保留原目录结构
├── raw/<sha256>.<ext>             # 原始字节（保真副本，不参与改写）
├── missing/index.html             # 未归档页面的占位/说明页
├── inventory/index.html           # 全量 URL 清单 + 状态（可搜索）
├── search/index.json              # 客户端搜索用的标题/URL 索引
├── 404.html
└── build-report.json              # 构建统计（链接解析率、缺失清单）
```

- 规范化 URL 用**语义化路径**（`/t/10128/2/`），便于静态托管，也避免 `viewthread.php?tid=..&page=..` 这类带查询串的路径问题。
- 保留 `raw/` 是为了「原始字节不可变」——改写只发生在 `site/` 里的副本上。
- 文件名冲突（不同原 URL 映射到同一路径）时追加短哈希后缀，并记入 `build-report.json`。

## 3. URL → 路径映射

用 `urls.canonical_key` 作为唯一真相：

| canonical_key | 输出路径 |
| --- | --- |
| `thread:<tid>:page:<n>` | `/t/<tid>/<n>/` |
| `forum:<fid>:page:<n>` | `/f/<fid>/<n>/` |
| `user:<uid>[:op]:page:<n>` | `/u/<uid>/[<op>/][<n>/]` |
| `article:<id>[:print\|:comment\|:mail]` | `/a/<id>/[print\|comment\|mail]/` |
| `attachment:<id>`、`image:*`、`avatar:*` | `/asset/<原路径或哈希>/<basename>` |
| `generic:<host><path>?<query>` | 保留原路径；带查询串的用 `<path>/<query-slug>/index.html` |
| 其他（css/js/font） | `/asset/<原路径>` |

同一 canonical_key 的**多个原始 URL**（如 `thread-10128-1-1.html` 与 `viewthread.php?tid=10128`）都指向同一输出路径 —— 这正是「把分立文件整理成一个站」的核心：**归一化后只留一份页面**。

## 4. 链接改写

对每个 HTML blob：

1. 解析 `href` / `src` / `action` / `background` / `<meta http-equiv=refresh>`。
2. 以「该页面的**原始 URL**」为 base 做相对路径解析（不是本地路径解析，必须用原始 URL，否则目录结构会错）。
3. 目标处理优先级：
   a. 目标 URL 在 `urls` 表中 → 直接指向其规范路径；
   b. 目标不在，但其 canonical_key 有已归档的兄弟页 → 指向规范路径（例如 `viewthread.php?tid=10128&page=2` → `/t/10128/2/`）；
   c. 目标属于 `suspected_gaps`（页序缺口）→ 指向 `/missing/?reason=gap&object=thread:10128&page=3`，说明「该页档案库未收录」；
   d. 其他未归档 → `/missing/?url=<原URL>&from=<本页时间戳>`。
4. **Discuz/ASP 专项规则**（这些是站内导航的主要形态）：
   - `redirect.php?goto=lastpost&tid=N` → `/t/N/<最大页>/`（或该帖最后已归档页）；
   - `forum.php?mod=viewthread&tid=N` / `viewthread.php?tid=N` → `/t/N/1/`；
   - `forum.php?mod=forumdisplay&fid=N` → `/f/N/1/`；
   - `space.php?uid=N` / `space-uid-N.html` → `/u/N/`；
   - `home.php?mod=space&uid=N&do=profile` → `/u/N/`；
   - `Article/Print.asp?ArticleID=N` → `/a/N/print/`；
   - `tag.php?name=X` → `/tag/<name>/`（额外生成标签聚合页）；
   - 分页链接（`page=N`）→ `/t/.. /N/`。
5. 外站链接（非 xiasl.net）：默认**保留原样但加 `rel="noopener"` 并标记 `data-external="1"`**；可由开关改为指向 `/missing/?reason=external`。
6. 改写后偏移量会变化，因此对 HTML 采用「解码 → 结构改写 → 重新编码」，而不是字节级替换。

## 5. 编码处理（重要）

站点原始页面是 **GB2312/GBK**，库里也存有被错误编码的 URL/作者名（如泰文乱码）。

- 依据 `<meta charset>` / `Content-Type` 声明或字节启发式判定编码；
- 解码为 Unicode → 改写 → **以 UTF-8 重新编码**，并注入 `<meta charset="utf-8">`；
- 无法判定编码时**退化为只改 ASCII 安全区域**（不整体重写），并记入报告；
- 乱码 URL（档案库自身写错的那批）在目录里标记 `data-mojibake="1"`，不影响其它页面。

## 6. 每页注入的可追溯信息

在每个页面底部注入（可关闭）一小段：

```html
<!-- archived by xiasl-archive: original=<原URL> captured=<时间戳>
     digest=<CDX digest> sha256=<blob> replay=<Wayback id_ URL> -->
<div class="archive-footer">
  本页为 Internet Archive 存档副本 · 原址 xiasl.net · 抓取于 2010-06-17 04:12
  · 查看原始快照 · 查看原始字节
</div>
```

→ 满足「所有文件都可追溯到 original URL + timestamp + digest」。

## 7. 导航与索引页（把「一堆页面」变成「一个站」）

1. **首页**：最新归档的站点首页（`xiasl.net/`），若缺失则合成一个入口页。
2. **版块列表**：按 `forum:<fid>` 汇总其下帖子（从 `logical_objects` + `links` 推导），生成 `/f/<fid>/index.html` 帖子目录。
3. **帖子内翻页**：`/t/<tid>/<n>/` 顶部加「第 n/m 页」导航；m 来自该帖已归档的最大页；缺页标注。
4. **站内搜索**：抽取每页 `<title>` + 首屏文本关键词，生成 `search/index.json`，配一个纯 JS 过滤页（无后端）。
5. **附件/图片画廊**：按帖子/版块聚合 `attachment`/`image` 资源。
6. **全量清单页**：`inventory/index.html` 列出所有 URL、状态（已归档/历史失败/未归档）、时间戳、大小，供人工检查缺页。

## 8. 未归档页面的体验

`missing/index.html` 统一承接，参数化显示：

- 原 URL、来源页；
- 「档案库没有这一页」的明确说明（区分：回放不存在 / 当年就抓到错误页 / 页序缺口 / 外站链接）；
- **替代入口**：该逻辑对象已归档的其它时间版本、相邻页（上一页/下一页）、所属版块列表、Wayback 原始快照外链；
- 可选「列出该站所有已归档时间点」——数据来自 `captures` 表。

## 9. 构建流程与验证

```
merge_archive.py ×4        # 四个来源合并到主库（按 sha256 校验，只搬运成功行）
build_static_site.py       # 读 state.sqlite3 + manifest → 生成 site/
verify_site.py             # 遍历 site/ 检查链接
```

- 构建是**幂等且可增量**的：以 (canonical_key, digest) 为缓存键，已生成的页面跳过。
- `verify_site.py` 产出：
  - 内部链接总数 / 成功解析数 / 指向 missing 的数量；
  - 每个资源类型的页面数与字节数；
  - 指向外站（未改写）的链接清单；
  - `raw/` 与 `blobs/` 的一致性校验（sha256）。
- 期望指标：正文类（thread/article/forum/user）内部链接解析率 > 90%；缺页链接全部有明确说明页。

## 10. 建议的实施阶段（待你确认后执行）

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| A | URL→路径映射 + 资源落盘 + 编码统一 | 可打开但链接仍指向原站 |
| B | 站内链接改写 + missing 页 + 每页溯源脚注 | 可点击浏览的离线站 |
| C | 索引页（首页/版块/帖子翻页）+ 搜索 + 清单页 | 完整导航 |
| D | 验证与报告 + 打包（zip/tar） | 交付物 |

## 11. 需要你决定的四件事

1. **外链策略**：保留原始外站链接，还是全部改写成「本页未归档」提示？（默认保留 + 标记）
2. **URL 风格**：语义化路径 `/t/10128/2/`（默认，干净）还是镜像原路径 `viewthread.php/tid=10128/page=2/`（更接近原站）？
3. **版本策略**：只放每个逻辑对象的最新版本（默认），还是把所有历史 digest 版本都放（体积和导航复杂度都会上升）？
4. **低价值资源**：priority 3–4 的 CSS/JS/统计脚本/广告是否也纳入站点（默认纳入，保证页面样式正常；可用过滤开关排除）。
