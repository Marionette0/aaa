"""
GitHub Actions 关键词搜索脚本（拆成 搜索 / 下载 / 汇总 三种模式，跑在不同 job 上）

工作流分三个 job（见 .github/workflows/download_search.yml）：
1. 搜索 job（SCRIPT_MODE=search）
   执行搜索，给每个结果**预先分配好打包名**（本子名，重名加 (1)、(2)...），写出：
   - albums.json   搜索结果清单（含打包名）
   - batches.json  每个本子一条（一个本子一个下载 job）
   - 初始 md（META_ONLY=是 时会补全详情，直接就是最终 md）
2. 下载 job（SCRIPT_MODE=download，matrix 并行，**一个本子一个 job**）
   下载这个本子，把图片按章节结构搬到上传暂存目录，并写一份 <aid>.json 状态。
   然后工作流把暂存目录作为产物上传，**产物名就是本子名**。
   因为每个 job 只下一个本子，所以这个 job 一跑完，它的产物立刻出现在网页的 Artifacts 区。
   ⚠ 下载模式下不再套内层 zip：GitHub 产物本身就是 zip，下载下来就是 `<本子名>.zip`，
   里面直接是章节文件夹和图片，没有嵌套压缩包。
3. 汇总 job（SCRIPT_MODE=merge）
   把所有 <aid>.json 合并成一个记录所有作品信息的 md。

本地单机跑（不拆 job）时用 SCRIPT_MODE=full（默认）：搜索 + 全部下载 + 出最终md。
本地没有 GitHub 产物机制，所以 full 模式会自己把每个本子打成 `<本子名>.zip`。

环境变量：
- SCRIPT_MODE:        search / download / merge / full，默认 full
- SEARCH_KEYWORD:     搜索关键词（search/full 模式必填）
- SEARCH_TYPE:        搜索范围（site/work/author/tag/actor），默认 site
- SEARCH_PAGE:        搜索结果的起始页码，默认 1
- SEARCH_PAGE_SIZE:   下载模式下要下载的搜索结果数量，默认 10
- SEARCH_ORDER_BY:    排序方式（mr/mv/mp/tf/tr/md），默认 mr
- SEARCH_TIME:        时间范围（a/t/w/m），默认 a
- SEARCH_CATEGORY:    本子类别（0/doujin/single/short/another/hanman/meiman/doujin_cosplay/3D/english_site），默认 0
- SEARCH_SUB_CATEGORY: 副分类（可选，网页端支持）
- META_ONLY:          是否只导出md（不下载图片/不打包），'否'/'是'，默认 '否'
- META_MAX:           META_ONLY=是 时最多收录的搜索结果数量（会翻页搜索），0 表示不限制，默认 100
- ALBUM_IDS:          download 模式要下载的本子id（多个用 - 或 , 分隔，实际只会传一个）
- JM_META_DIR:        albums.json 所在目录，默认与 JM_DOWNLOAD_DIR 相同
- JM_UPLOAD_DIR:      download 模式的上传暂存目录，默认 <JM_DOWNLOAD_DIR>/upload
- DELETE_AFTER_ZIP:   仅 full 模式用：打包成功后是否删除原始文件夹，'是'/'否'，默认 '是'

以下变量与下载工作流共用，见 workflow_download.py：
- CLIENT_IMPL / DIR_RULE / IMAGE_SUFFIX / IMAGE_QUALITY / PDF_OPTION / PDF_NAME_RULE
"""
import json
import os
import re
import shutil
import threading

from jmcomic import *

from workflow_download import env, get_option

# 搜索范围 -> client 上的搜索方法
SEARCH_METHOD_MAP = {
    'site': 'search_site',
    'work': 'search_work',
    'author': 'search_author',
    'tag': 'search_tag',
    'actor': 'search_actor',
}

ALBUMS_FILE = 'albums.json'
STATUS_DIR = 'status'


def env_choice(name, default):
    """
    读取工作流的 choice 输入。
    GitHub Actions 的 choice 值形如 '值 | 描述'，这里只取 '值' 部分。
    """
    value = env(name, default)
    return value.split('|')[0].strip()


def env_int(name, default):
    value = env(name, default)
    try:
        return int(value)
    except ValueError:
        ExceptionTool.raises(f'{name} 必须是整数，当前值为: {value}')


def env_ids(name) -> list:
    """解析本子id列表（用 - 或 , 或空白分隔）"""
    text = env(name, '') or ''
    return [part for part in re.split(r'[^0-9A-Za-z]+', text) if part]


def read_json(filepath, default=None):
    if not os.path.exists(filepath):
        return default
    with open(filepath, encoding='utf-8') as f:
        return json.load(f)


def write_json(filepath, data):
    mkdir_if_not_exists(os.path.dirname(filepath))
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def md_escape(text) -> str:
    """转义 markdown 表格中的特殊字符"""
    return str(text).replace('|', '\\|').replace('\n', ' ').strip()


def truncate_name(name: str, max_bytes: int = 180) -> str:
    """按 utf-8 字节数截断文件名，避免超出文件系统限制"""
    raw = name.encode('utf-8')
    if len(raw) <= max_bytes:
        return name
    return raw[:max_bytes].decode('utf-8', errors='ignore')


def decide_package_base_name(title, aid) -> str:
    """
    用本子名作为打包名（不含后缀），去掉文件名里的非法字符并兜底。

    这个名字会被用作 GitHub 产物的名字，所以必须是安全的文件名。
    """
    name = fix_windir_name(title or '').strip(' .')
    if not name:
        name = f'JM{aid}'
    return truncate_name(name)


def allocate_package_name(base_name: str, used_names: set) -> str:
    """分配打包名，重名时加 (1)、(2)..."""
    name = base_name
    if name in used_names:
        index = 1
        while f'{base_name}({index})' in used_names:
            index += 1
        name = f'{base_name}({index})'
    used_names.add(name)
    return name


def zip_has_entry(zip_path) -> bool:
    """检查zip里是否有文件（空zip说明原图在打包前就没了）"""
    import zipfile

    if not os.path.exists(zip_path):
        return False

    try:
        with zipfile.ZipFile(zip_path) as zf:
            return len(zf.namelist()) > 0
    except Exception:
        return False


def album_to_dict(album) -> dict:
    """把本子实体转成可 json 序列化的 dict（跨 job 传递用）"""
    return {
        'aid': str(album.album_id),
        'name': album.name,
        'authors': list(album.authors),
        'works': list(album.works),
        'actors': list(album.actors),
        'tags': list(album.tags),
        'pub_date': album.pub_date,
        'update_date': album.update_date,
        'page_count': album.page_count,
        'views': album.views,
        'likes': album.likes,
        'comment_count': album.comment_count,
        'description': album.description,
        'episodes': [[str(pid), str(pindex), str(ptitle)] for pid, pindex, ptitle in album.episode_list],
    }


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

class ZipSummary:
    """
    记录每个本子的下载/打包结果（本子之间并发下载时加锁）。
    """

    def __init__(self, preassigned_names=None):
        self.lock = threading.Lock()
        self.records = {}  # album_id -> dict(album, image_count, package, error)
        self.preassigned_names = dict(preassigned_names or {})
        self.used_names = set()

    def update(self, aid, **fields):
        with self.lock:
            record = self.records.setdefault(str(aid), {})
            record.update(fields)

    def allocate_package(self, aid, base_name: str) -> str:
        """优先用搜索 job 预先分配好的打包名，保证跨 job 命名一致"""
        with self.lock:
            name = self.preassigned_names.get(str(aid))
            if name is not None:
                return name

            name = allocate_package_name(base_name, self.used_names)
            self.preassigned_names[str(aid)] = name
            return name

    def get(self, aid):
        with self.lock:
            return dict(self.records.get(str(aid)) or {})


def stage_album_for_upload(option, album, upload_dir) -> int:
    """
    把本子目录里的内容搬到上传暂存目录，让产物解压后直接是章节文件夹（不套一层本子目录）。

    :return: 搬过去的条目数
    """
    album_root = option.dir_rule.decide_album_root_dir(album)
    if not os.path.isdir(album_root):
        return 0

    mkdir_if_not_exists(upload_dir)
    moved = 0

    for name in sorted(os.listdir(album_root)):
        src = os.path.join(album_root, name)
        dst = os.path.join(upload_dir, name)

        if os.path.exists(dst):
            # 同名兜底（正常不会发生：一个 job 只下一个本子）
            index = 1
            while os.path.exists(f'{dst}({index})'):
                index += 1
            dst = f'{dst}({index})'

        shutil.move(src, dst)
        moved += 1

    return moved


def collect_album_results(batch_result, summary) -> None:
    """从 BatchResult 里取出每个本子的详情和图片数，并落实产物名，写进 summary"""
    for result in batch_result:
        album = result.detail
        downloader = result.downloader
        aid = str(album.album_id)

        image_count = sum(
            len(image_list)
            for image_list in downloader.download_success_dict.get(album, {}).values()
        )
        package = summary.allocate_package(aid, decide_package_base_name(album.name, aid))

        summary.update(aid, album=album_to_dict(album), image_count=image_count, package=package)


class AlbumZipPlugin(ZipPlugin):
    """
    【仅本地 full 模式使用】每个本子下载完成后立即打包为 `<打包名>.zip`。

    复用 jmcomic 内置 ZipPlugin 的压缩逻辑：
    - 挂在 after_album 上，所以是「下载完一个本子就打包一个」
    - zip 内保留章节文件夹结构（图片相对本子根目录存放，与下载目录一致）

    注意：GitHub Actions 的 download 模式不用这个插件——产物本身就是 zip，
    再套一层内层 zip 就变成压缩包嵌套了。
    """

    plugin_key = 'workflow_album_zip'

    def invoke(self,
               downloader,
               album=None,
               summary=None,
               delete_original_file=False,
               suffix='zip',
               zip_dir='./',
               filename_rule=None,
               level=None,
               dir_rule=None,
               encrypt=None,
               ) -> None:
        ExceptionTool.require_true(summary is not None, '插件参数 summary 不能为空')

        aid = str(album.album_id)
        image_count = sum(
            len(image_list)
            for image_list in downloader.download_success_dict.get(album, {}).values()
        )
        summary.update(aid, album=album_to_dict(album), image_count=image_count)

        if image_count == 0:
            summary.update(aid, error='没有下载到任何图片，跳过打包')
            return

        self.summary = summary
        self.package = summary.allocate_package(
            aid, decide_package_base_name(album.name, aid),
        )

        try:
            super().invoke(
                downloader,
                album=album,
                photo=None,
                delete_original_file=delete_original_file,
                level=level,
                filename_rule=filename_rule,
                suffix=suffix,
                zip_dir=zip_dir,
                dir_rule=dir_rule,
                encrypt=encrypt,
            )
        except BaseException as e:
            summary.update(aid, error=f'打包失败: {e}')
            raise

        zip_path = os.path.join(zip_dir, f'{self.package}.zip')
        if not zip_has_entry(zip_path):
            # 兜底：zip 里没有任何条目说明原图在打包前就没了（例如被别的插件删掉），
            # 这种情况删掉空包并记录异常，避免留一个空zip误导人
            if os.path.exists(zip_path):
                os.remove(zip_path)
            msg = '打包结果为空，原图可能在打包前已被其他插件删除（例如PDF合并插件）'
            jm_log('search.zip', f'JM{aid} {msg}')
            summary.update(aid, error=msg)
            return

        summary.update(aid, package=self.package)

    def decide_filepath(self, album, photo, filename_rule, suffix, base_dir, dir_rule_dict):
        """用本子名（而不是 dir_rule 规则）来决定压缩包路径"""
        base_dir = base_dir or os.getcwd()
        mkdir_if_not_exists(base_dir)
        return fix_filepath(os.path.join(base_dir, f'{self.package}.zip'))


def download_and_zip(album_ids, option, download_dir, summary, delete_after_zip=True):
    """
    【仅本地 full 模式】下载本子，每个本子下载完成后立即打包。

    :return: BatchResult（含 .failed）
    """
    JmModuleConfig.register_plugin(AlbumZipPlugin)

    after_album = option.plugins.setdefault('after_album', [])
    if isinstance(after_album, list) and not any(
            (pinfo or {}).get('plugin') == AlbumZipPlugin.plugin_key for pinfo in after_album):
        # 插到最前面：保证先打包zip再执行其他 after_album 插件
        # （例如 img2pdf 插件会删除原图，若它先跑，打包出来的zip就是空的）
        after_album.insert(0, {
            'plugin': AlbumZipPlugin.plugin_key,
            'kwargs': {
                'zip_dir': download_dir,
                'summary': summary,
                'delete_original_file': delete_after_zip,
                'suffix': 'zip',
            },
        })

    jm_log('search', f'开始下载 {len(album_ids)} 个本子，每下载完一个立即打包: {album_ids}')
    batch_result = download_album(album_ids, option)
    option.call_all_plugin('after_download')
    return batch_result


def write_status_files(target_dir, album_ids, summary, failed_map):
    """每个本子写一份状态文件，供汇总 job 合并 md"""
    mkdir_if_not_exists(target_dir)

    for aid in album_ids:
        aid = str(aid)
        record = summary.get(aid)

        error = record.get('error')
        if error is None and aid in failed_map:
            error = f'下载失败: {failed_map[aid]}'
        if error is None and record.get('package') is None:
            error = '未完成下载'

        write_json(os.path.join(target_dir, f'{aid}.json'), {
            'aid': aid,
            'package': record.get('package'),
            'image_count': record.get('image_count'),
            'error': error,
            'album': record.get('album'),
        })


# ---------------------------------------------------------------------------
# md
# ---------------------------------------------------------------------------

def fetch_album_info(client, aid):
    """请求本子详情，失败返回 None（不影响其他结果）"""
    try:
        return album_to_dict(client.get_album_detail(aid))
    except Exception as e:
        jm_log('search.md', f'获取本子详情失败: JM{aid}, 异常: [{e}]')
        return None


def build_records(albums, status_map=None, details=None, expect_download=False):
    """
    albums:    [{'aid','title','tags','package'}]
    status_map: aid -> status dict（下载job写的）
    details:   aid -> 本子详情 dict（只导出md模式下请求到的）
    """
    status_map = status_map or {}
    details = details or {}
    records = []

    for item in albums:
        aid = str(item['aid'])
        status = status_map.get(aid) or {}
        album = status.get('album') or details.get(aid)

        error = status.get('error')
        if error is None and expect_download and album is None:
            error = '未完成下载'
        if error is None and album is None and details:
            error = '本子详情获取失败'

        records.append({
            'aid': aid,
            'title': item.get('title') or f'JM{aid}',
            'tags': item.get('tags') or [],
            'package': status.get('package'),
            'plan_package': item.get('package'),
            'image_count': status.get('image_count'),
            'error': error,
            'album': album,
        })

    return records


def write_search_md(keyword, search_type, order_by, time_, category, records, filepath, mode):
    """写出记录所有作品信息的 md"""
    lines = []
    add = lines.append

    packed_records = [r for r in records if r.get('package')]
    error_records = [r for r in records if r.get('error')]

    add(f'# 搜索结果: {keyword}')
    add('')
    add(f'> 本文件由 jmcomic GitHub Actions 自动生成（模式: {mode}）。')
    add('')
    add('## 搜索参数')
    add('')
    add('| 参数 | 值 |')
    add('| --- | --- |')
    add(f'| 关键词 | {md_escape(keyword)} |')
    add(f'| 搜索范围 | {md_escape(search_type)} |')
    add(f'| 排序 | {md_escape(order_by)} |')
    add(f'| 时间范围 | {md_escape(time_)} |')
    add(f'| 类别 | {md_escape(category)} |')
    add(f'| 结果数量 | {len(records)} |')
    add(f'| 打包数量 | {len(packed_records)} |')
    add(f'| 异常数量 | {len(error_records)} |')
    add(f'| 生成时间 | {time_stamp()} |')
    add('')
    add('## 目录')
    add('')
    for index, record in enumerate(records, start=1):
        add(f'{index}. [JM{record["aid"]} - {md_escape(record["title"])}](#jm{record["aid"]})')
    add('')

    for record in records:
        aid = record['aid']
        add(f'<a id="jm{aid}"></a>')
        add(f'## JM{aid} - {md_escape(record["title"])}')
        add('')

        # 搜索页自带的基础信息
        add(f'- ID: {aid}')
        add(f'- 链接: https://18comic.vip/album/{aid}/')
        add(f'- 搜索标签: {md_escape(", ".join(record.get("tags") or []))}')
        if record.get('package'):
            add(f'- 打包产物: {record["package"]}.zip')
        elif record.get('plan_package'):
            add(f'- 预定打包名: {record["plan_package"]}.zip（未生成）')
        if record.get('image_count') is not None:
            add(f'- 图片数量: {record["image_count"]}')
        add('')

        album = record.get('album')
        if album is None:
            if record.get('error'):
                add(f'- ⚠️ {md_escape(record["error"])}')
                add('')
            continue

        add('| 字段 | 值 |')
        add('| --- | --- |')
        add(f'| 标题 | {md_escape(album.get("name", ""))} |')
        add(f'| 作者 | {md_escape(", ".join(album.get("authors") or []))} |')
        add(f'| 作品 | {md_escape(", ".join(album.get("works") or []))} |')
        add(f'| 登场角色 | {md_escape(", ".join(album.get("actors") or []))} |')
        add(f'| 标签 | {md_escape(", ".join(album.get("tags") or []))} |')
        add(f'| 发布日期 | {md_escape(album.get("pub_date", ""))} |')
        add(f'| 更新日期 | {md_escape(album.get("update_date", ""))} |')
        add(f'| 总页数 | {album.get("page_count", "")} |')
        add(f'| 观看 | {md_escape(album.get("views", ""))} |')
        add(f'| 点赞 | {md_escape(album.get("likes", ""))} |')
        add(f'| 评论数 | {album.get("comment_count", "")} |')
        add(f'| 简介 | {md_escape(album.get("description", ""))} |')
        add('')

        if record.get('error'):
            add(f'- ⚠️ {md_escape(record["error"])}')
            add('')

        episodes = album.get('episodes') or []
        if len(episodes) > 0:
            add('### 章节')
            add('')
            for pid, pindex, ptitle in episodes:
                add(f'- 第{pindex}话 (JM{pid}): {md_escape(ptitle)}')
            add('')

    add('---')
    add('')
    add(f'共 {len(records)} 个结果，成功打包 {len(packed_records)} 个，异常 {len(error_records)} 个。')

    mkdir_if_not_exists(os.path.dirname(filepath))
    write_text(filepath, '\n'.join(lines))


# ---------------------------------------------------------------------------
# 各模式的实现
# ---------------------------------------------------------------------------

def search_and_collect(client, search_method, keyword, start_page, order_by,
                       time_, category, sub_category, max_results):
    """
    翻页搜索，收集 (album_id, album_title, album_tags) 列表。

    :param max_results: 最多收集数量，None 表示不限制
    """
    collected = []
    page = start_page

    while True:
        search_page = search_method(
            keyword,
            page=page,
            order_by=order_by,
            time=time_,
            category=category,
            sub_category=sub_category,
        )

        jm_log('search',
               f'第{page}页: 本页结果[{len(search_page)}], 总数[{search_page.total}], 页数[{search_page.page_count}]')

        for aid, atitle, tags in search_page.iter_id_title_tag():
            collected.append((aid, atitle, tags))
            jm_log('search', f'命中: JM{aid} | {atitle} | {tags}')

            if max_results is not None and len(collected) >= max_results:
                break

        if max_results is not None and len(collected) >= max_results:
            break

        # 没有更多页了
        if search_page.page_number is None or search_page.page_number >= search_page.page_count:
            break

        page += 1

    return collected


class SearchContext:
    """一次运行里各模式共用的参数与路径"""

    def __init__(self):
        self.mode = env('SCRIPT_MODE', 'full').strip().lower()
        self.keyword = env('SEARCH_KEYWORD', None)
        self.search_type = env_choice('SEARCH_TYPE', 'site')
        self.page = env_int('SEARCH_PAGE', 1)
        self.page_size = env_int('SEARCH_PAGE_SIZE', 10)
        self.order_by = env_choice('SEARCH_ORDER_BY', JmMagicConstants.ORDER_BY_LATEST)
        self.time_ = env_choice('SEARCH_TIME', JmMagicConstants.TIME_ALL)
        self.category = env_choice('SEARCH_CATEGORY', JmMagicConstants.CATEGORY_ALL)
        self.sub_category = env('SEARCH_SUB_CATEGORY', None) or None

        self.meta_only = env_choice('META_ONLY', '否') == '是'
        self.meta_max = env_int('META_MAX', 100)
        self.delete_after_zip = env_choice('DELETE_AFTER_ZIP', '是') == '是'
        self.album_ids = env_ids('ALBUM_IDS')

        if self.meta_max <= 0:
            self.meta_max = None

        if self.search_type not in SEARCH_METHOD_MAP:
            ExceptionTool.raises(
                f'不支持的搜索类型: [{self.search_type}]，可选值: {list(SEARCH_METHOD_MAP)}'
            )

        self.download_dir = env('JM_DOWNLOAD_DIR', workspace())
        self.meta_dir = env('JM_META_DIR', None) or self.download_dir
        self.upload_dir = env('JM_UPLOAD_DIR', None) or os.path.join(self.download_dir, 'upload')
        self.status_dir = os.path.join(self.download_dir, STATUS_DIR)
        self.md_path = os.path.join(self.download_dir, f'搜索结果-{fix_windir_name(self.keyword or "无关键词")}.md')
        self.mode_label = '只导出md（不下载图片）' if self.meta_only else '下载并按本子打包'


def cmd_search(ctx: SearchContext):
    """搜索 + 预先分配打包名 + 写 albums.json / batches.json / 初始md"""
    ExceptionTool.require_true(ctx.keyword, '未配置搜索关键词，请填入 SEARCH_KEYWORD')

    option = get_option()
    client = option.new_jm_client()
    search_method = getattr(client, SEARCH_METHOD_MAP[ctx.search_type])

    # 只导出md模式：翻页收录到 META_MAX；下载模式：只收录 SEARCH_PAGE_SIZE 个
    max_results = ctx.meta_max if ctx.meta_only else ctx.page_size
    result_list = search_and_collect(
        client, search_method, ctx.keyword, ctx.page,
        ctx.order_by, ctx.time_, ctx.category, ctx.sub_category,
        max_results,
    )

    if len(result_list) == 0:
        jm_log('search', '没有搜索到任何结果，本次运行结束。')
        write_json(os.path.join(ctx.meta_dir, ALBUMS_FILE), _meta_dict(ctx, []))
        write_json(os.path.join(ctx.meta_dir, 'batches.json'), [])
        return

    # 预先分配打包名（单线程，保证跨 job 命名一致、重名加(n)）
    used_names = set()
    albums = []
    for aid, atitle, tags in result_list:
        albums.append({
            'aid': str(aid),
            'title': atitle,
            'tags': list(tags),
            'package': allocate_package_name(decide_package_base_name(atitle, aid), used_names),
        })

    write_json(os.path.join(ctx.meta_dir, ALBUMS_FILE), _meta_dict(ctx, albums))

    # 一个本子一个下载 job（产物名必须是本子名，所以不能一批多个）
    batches = [
        {'index': index + 1, 'ids': item['aid'], 'name': item['package'], 'title': item['title']}
        for index, item in enumerate(albums)
    ]
    write_json(os.path.join(ctx.meta_dir, 'batches.json'), batches)
    jm_log('search',
           f'共 {len(albums)} 个本子，将启动 {len(batches)} 个下载job（一个本子一个job，产物名=本子名）')

    # 初始 md：只导出md模式下会补全详情，直接就是最终结果
    details = {}
    if ctx.meta_only:
        for item in albums:
            details[item['aid']] = fetch_album_info(client, item['aid'])

    records = build_records(albums, details=details)
    write_search_md(
        ctx.keyword, ctx.search_type, ctx.order_by, ctx.time_, ctx.category,
        records, ctx.md_path, ctx.mode_label,
    )
    jm_log('search.md', f'已生成md文件: {ctx.md_path}')


def _meta_dict(ctx: SearchContext, albums: list) -> dict:
    return {
        'keyword': ctx.keyword,
        'search_type': ctx.search_type,
        'order_by': ctx.order_by,
        'time': ctx.time_,
        'category': ctx.category,
        'mode_label': ctx.mode_label,
        'albums': albums,
    }


def load_meta(ctx: SearchContext) -> dict:
    meta = read_json(os.path.join(ctx.meta_dir, ALBUMS_FILE), None)
    ExceptionTool.require_true(meta is not None, f'未找到搜索元信息: {os.path.join(ctx.meta_dir, ALBUMS_FILE)}')
    return meta


def cmd_download(ctx: SearchContext):
    """
    下载这个本子 → 图片搬到上传暂存目录（产物解压后直接是章节文件夹）

    不套内层 zip：GitHub 产物本身就是一个 zip，产物名 = 本子名，
    所以用户下载到的就是 `<本子名>.zip`，里面直接是图片，无嵌套压缩包。
    """
    meta = load_meta(ctx)
    album_map = {str(item['aid']): item for item in meta.get('albums') or []}

    album_ids = ctx.album_ids
    if len(album_ids) == 0:
        jm_log('search', '本批没有要下载的本子，跳过')
        return

    ExceptionTool.require_true(
        all(aid in album_map for aid in album_ids),
        f'本批id不在搜索元信息中: {[aid for aid in album_ids if aid not in album_map]}'
    )

    summary = ZipSummary({aid: album_map[aid]['package'] for aid in album_ids})
    option = get_option()

    jm_log('search', f'开始下载 {len(album_ids)} 个本子: {album_ids}')
    batch_result = download_album(album_ids, option)

    failed_map = dict(getattr(batch_result, 'failed', None) or {})
    collect_album_results(batch_result, summary)

    # 把图片搬到上传暂存目录：产物解压后直接是章节文件夹，不套一层本子目录
    for aid in album_ids:
        record = summary.get(aid)
        album_info = record.get('album')
        if album_info is None:
            continue

        album = _album_from_dict(album_info)
        moved = stage_album_for_upload(option, album, ctx.upload_dir)
        jm_log('search', f'JM{aid} 已暂存 {moved} 个条目到上传目录: {ctx.upload_dir}')

    # 状态文件单独放，作为独立的小产物上传（避免汇总job去下载一大堆图片）
    write_status_files(ctx.status_dir, album_ids, summary, failed_map)

    for aid in album_ids:
        record = summary.get(aid)
        jm_log('search', f'JM{aid} 完成: 产物名=[{record.get("package")}], '
                         f'图片={record.get("image_count")}, 异常={record.get("error")}')


def _album_from_dict(data: dict):
    """
    用 dict 还原一个本子实体（只为拿到 dir_rule 需要的字段：
    album_id / authors / name / episode_list）。
    """
    return JmModuleConfig.album_class()(
        album_id=data.get('aid'),
        scramble_id='0',
        name=data.get('name') or '',
        episode_list=[tuple(episode) for episode in (data.get('episodes') or [])],
        page_count=data.get('page_count') or 0,
        pub_date=data.get('pub_date') or '',
        update_date=data.get('update_date') or '',
        likes=data.get('likes') or '',
        views=data.get('views') or '',
        comment_count=data.get('comment_count') or 0,
        works=list(data.get('works') or []),
        actors=list(data.get('actors') or []),
        authors=list(data.get('authors') or []),
        tags=list(data.get('tags') or []),
        description=data.get('description') or '',
    )


def _load_status_map(root_dir) -> dict:
    """递归扫描状态文件（下载job会把 <aid>.json 放进各自产物的根目录）"""
    status_map = {}
    if not os.path.isdir(root_dir):
        return status_map

    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for name in filenames:
            if not name.endswith('.json'):
                continue

            data = read_json(os.path.join(dirpath, name), None)
            if isinstance(data, dict) and data.get('aid') and 'album' in data:
                status_map[str(data['aid'])] = data

    return status_map


def cmd_merge(ctx: SearchContext):
    """合并 albums.json + 所有状态文件，写出最终 md"""
    meta = load_meta(ctx)
    albums = meta.get('albums') or []

    status_map = _load_status_map(ctx.download_dir)
    if os.path.isdir(ctx.status_dir):
        status_map.update(_load_status_map(ctx.status_dir))

    records = build_records(albums, status_map=status_map, expect_download=True)
    write_search_md(
        meta.get('keyword') or ctx.keyword,
        meta.get('search_type') or ctx.search_type,
        meta.get('order_by') or ctx.order_by,
        meta.get('time') or ctx.time_,
        meta.get('category') or ctx.category,
        records, ctx.md_path, meta.get('mode_label') or ctx.mode_label,
    )

    packed = len([r for r in records if r.get('package')])
    jm_log('search', f'汇总完成: 共 {len(records)} 个结果, 打包 {packed} 个, md: {ctx.md_path}')


def cmd_full(ctx: SearchContext):
    """本地单机：搜索 + 全部下载 + 每个本子打成zip + 出最终md（没有GitHub产物机制，所以自己打包）"""
    cmd_search(ctx)

    meta = load_meta(ctx)
    albums = meta.get('albums') or []
    if len(albums) == 0 or ctx.meta_only:
        return

    album_ids = [str(item['aid']) for item in albums]
    summary = ZipSummary({item['aid']: item['package'] for item in albums})
    option = get_option()

    batch_result = download_and_zip(album_ids, option, ctx.download_dir, summary, ctx.delete_after_zip)
    failed_map = dict(getattr(batch_result, 'failed', None) or {})
    write_status_files(ctx.status_dir, album_ids, summary, failed_map)

    cmd_merge(ctx)


def main():
    ctx = SearchContext()

    handlers = {
        'search': cmd_search,
        'download': cmd_download,
        'merge': cmd_merge,
        'full': cmd_full,
    }

    handler = handlers.get(ctx.mode, None)
    ExceptionTool.require_true(
        handler is not None,
        f'不支持的 SCRIPT_MODE: [{ctx.mode}]，可选值: {list(handlers)}'
    )

    jm_log('search', f'SCRIPT_MODE={ctx.mode}, 关键词=[{ctx.keyword}], '
                     f'下载目录=[{ctx.download_dir}], 上传目录=[{ctx.upload_dir}]')
    handler(ctx)


if __name__ == '__main__':
    main()
