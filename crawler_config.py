"""Proj01.py 的用户可编辑设置。

这些值从 inform.json 中导出的浏览器 fetch 记录整理而来。
运行时命令行参数仍然有效，并会临时覆盖这些值。
"""



# 首次运行应保持较小的数量。确认文件名后将 LIMIT 设为 None。
LIMIT = None

# inform.json 中没有导出 Cookie 值，只能看到 credentials="include"。
# 运行前仍需把浏览器 Cookie 另存为 cookies.json。
COOKIE_JSON = "cookies.json"

# 站点和后台入口。
SITE_BASE_URL = "https://kjxyjy.91wllm.cn/"
ADMIN_INDEX_URL = "https://kjxyjy.91wllm.cn/admin/default/index"

# inform.json 中实际加载 #jinfo-grid 表格的 AJAX 地址。
# 去掉了浏览器缓存参数 &_=1782801523409，避免配置随一次抓包时间戳变化。
JINFO_LIST_URL = (
    "https://kjxyjy.91wllm.cn/admin/tempdb/jinfo/list/"
    "universityid/1577/grade/2026/sinfo_verify055/4/formal/0"
    "?target=navTab"
)

# Proj01.py 读取的抓取入口。
START_URL = JINFO_LIST_URL

# inform.json 中另一个统计接口，目前 Proj01.py 不直接使用，保留作排查参考。
TONGJI_URL = "https://kjxyjy.91wllm.cn/admin/tempdb/jinfo/tongji/formal/1?target=navTab"

# 从 inform.json 中提取的稳定 AJAX 请求头。浏览器自动生成的 sec-* 头不放入配置。
AJAX_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "x-requested-with": "XMLHttpRequest",
}

# 下载和日志输出目录。
OUT_DIR = (
    "C:/_zhangyidaFiles/C02_Study/207_Projects/"
    "Project250515_nodejs/001_kaoyan/PaChong/data"
)

# 该站点目前在命令行客户端中可能存在证书链问题。
INSECURE = True

# 设置为 True 则会在抓取时显示浏览器窗口。
HEADFUL = False

# 页面/表格选择器。
GRID_SELECTOR = "#jinfo-grid table tbody tr"
NEXT_SELECTOR = None

# 分页控制。
NO_PAGINATION = False
MAX_PAGES = 200


# Playwright 超时时间（毫秒）。
TIMEOUT_MS = 30000

# 基于 1 的表格列号。设为 None 表示在可能时从表头自动检测。
CLASS_COL = None
STUDENT_ID_COL = None
NAME_COL = None

# 当前就业信息页可从表头“上传就业相关证明”自动识别证明列。
ATTACHMENT_COL = None
