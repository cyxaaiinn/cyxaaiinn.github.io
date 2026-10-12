#!/usr/bin/env python3
"""
飞书多维表格 -> 作品集站点 数据刷新脚本

三个开关的控制点都在这里（这是关键：开关必须做在数据层，不能只做在前端）：

  1. 网站总开关 —— 环境变量 SITE_OPEN
       true  : 正常输出作品数据
       false : 只输出一个"维护中"的数据文件，作品数据完全不写入。
               注意这不是前端隐藏，是数据根本没生成 —— 别人直接访问
               api/portfolio.json 也拿不到任何作品。

  2. 单条作品开关 —— 表格里的「上架」列
       勾上   : 出现在网站上
       没勾   : 不进 JSON，网站上完全不存在（同样不是前端隐藏）
       表格没有这一列 : 全部视为上架，并打印提醒

  3. 刷新模式（自动/手动）—— 由 workflow 判断，本脚本不感知。

依赖：仅 Python 标准库，无第三方包。
用法：
    python refresh.py             # 正常刷新（需要下面 4 个环境变量）
    python refresh.py --selftest  # 用内置假数据跑一遍，验证输出逻辑

环境变量：
    LARK_APP_ID / LARK_APP_SECRET / LARK_BASE_TOKEN / LARK_TABLE_ID
    SITE_OPEN（可选，默认 true）
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 配置

ROOT = Path(__file__).resolve().parent
API_DIR = ROOT / "api"
PORTFOLIO_FILE = API_DIR / "portfolio.json"
VIDEOS_FILE = API_DIR / "videos.json"
COVERS_FILE = API_DIR / "covers.json"

APP_ID = os.environ.get("LARK_APP_ID", "").strip()
APP_SECRET = os.environ.get("LARK_APP_SECRET", "").strip()
BASE_TOKEN = os.environ.get("LARK_BASE_TOKEN", "").strip()
TABLE_ID = os.environ.get("LARK_TABLE_ID", "").strip()
SITE_OPEN = os.environ.get("SITE_OPEN", "true").strip().lower() not in ("false", "0", "no", "off")

# 字段名候选：表格里叫哪个都能认，认不出会打印提醒
FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "video":    ("样片", "视频", "作品", "附件", "视频作品"),
    "cover":    ("封面", "封面图", "海报", "图片", "缩略图"),
    "title":    ("内容", "标题", "作品名称", "名称"),
    "category": ("类型", "分类", "标签"),
    "duration": ("时长", "片长"),
    "tools":    ("AI工具", "工具", "使用工具"),
    "order":    ("序号", "排序", "order", "Order"),
    "shelf":    ("上架", "展示", "发布", "上线", "对外"),
}

BATCH_SIZE = 5          # 飞书批量取链接接口单次上限
OFF_VALUES = ("false", "0", "no", "off", "否", "不下架", "下架", "隐藏", "不展示")


# ---------------------------------------------------------------- 工具

def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> None:
    print("错误: " + msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_json(url: str, *, data: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        die(f"HTTP {exc.code} 调用 {url} 失败：{detail}")
    except urllib.error.URLError as exc:
        die(f"请求 {url} 失败：{exc}")
    return {}


# ---------------------------------------------------------------- 解析

def pick(fields: dict[str, Any], key: str) -> Any:
    for name in FIELD_CANDIDATES[key]:
        if name in fields and fields[name] not in (None, "", []):
            return fields[name]
    return None


def has_column(fields_list: list[dict[str, Any]], key: str) -> bool:
    names = FIELD_CANDIDATES[key]
    for fields in fields_list:
        for n in names:
            if n in fields:
                return True
    return False


def text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for k in ("text", "name", "value", "en_name"):
            if isinstance(value.get(k), str):
                return value[k].strip()
        return ""
    if isinstance(value, list):
        return ", ".join(p for p in (text_of(v) for v in value) if p)
    return str(value).strip()


def list_of_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [t for t in (text_of(v) for v in value) if t]
    t = text_of(value)
    return [t] if t else []


def first_file_token(value: Any) -> str | None:
    """附件字段取第一个 file_token。"""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("file_token"):
                return str(item["file_token"])
    if isinstance(value, dict) and value.get("file_token"):
        return str(value["file_token"])
    return None


def parse_shelf(value: Any, column_exists: bool) -> bool:
    """「上架」列的判定。

    安全取向：表格有这一列但没填 -> 视为未上架（宁可漏，不可误上）。
    表格压根没这一列 -> 全部视为上架（否则整个站会空掉）。
    """
    if not column_exists:
        return True
    if value is None or value == "" or value == []:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in OFF_VALUES
    if isinstance(value, dict):
        return parse_shelf(value.get("text", value.get("name", value.get("value"))), True)
    if isinstance(value, list):
        return any(parse_shelf(v, True) for v in value)
    return bool(value)


def to_order(value: Any) -> float:
    try:
        return float(text_of(value))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 飞书接口

def tenant_token() -> str:
    res = request_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data={"app_id": APP_ID, "app_secret": APP_SECRET},
    )
    if res.get("code") != 0 or not res.get("tenant_access_token"):
        die("换取 tenant_access_token 失败：" + json.dumps(res, ensure_ascii=False))
    return str(res["tenant_access_token"])


def fetch_records(token: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = ""
    while True:
        params = {"page_size": 500}
        if page:
            params["page_token"] = page
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}"
            f"/tables/{TABLE_ID}/records?" + urllib.parse.urlencode(params)
        )
        res = request_json(url, token=token)
        if res.get("code") != 0:
            die("读取多维表格记录失败：" + json.dumps(res, ensure_ascii=False))
        data = res.get("data", {})
        out.extend(data.get("items", []))
        if not data.get("has_more") or not data.get("page_token"):
            break
        page = data["page_token"]
    return out


def resolve_urls(token: str, file_tokens: list[str]) -> dict[str, str]:
    """file_token -> 临时下载链接。"""
    resolved: dict[str, str] = {}
    uniq = [t for t in dict.fromkeys(file_tokens) if t]
    extra = urllib.parse.quote(json.dumps({"bitablePerm": {"tableId": TABLE_ID}}, ensure_ascii=False))
    base = "https://open.feishu.cn/open-apis/drive/v1/medias/batch_get_tmp_download_url"

    for i in range(0, len(uniq), BATCH_SIZE):
        batch = uniq[i:i + BATCH_SIZE]
        qs = ["extra=" + extra] + ["file_tokens=" + urllib.parse.quote(t) for t in batch]
        res = request_json(base + "?" + "&".join(qs), token=token)
        if res.get("code") != 0:
            die("解析媒体链接失败：" + json.dumps(res, ensure_ascii=False))
        for item in res.get("data", {}).get("tmp_download_urls", []):
            ft, u = item.get("file_token"), item.get("tmp_download_url")
            if ft and u:
                resolved[str(ft)] = str(u)
    return resolved


def fetch_field_names(token: str) -> set[str]:
    """读数据表的真实列名。

    必须走「列出字段」接口，不能靠记录里的 key 反推：
    飞书会**省略值为空的字段**，所以当「上架」这一列没人勾过时，
    每条记录的 fields 里都不会出现它 —— 反推就会得出"没有这一列"，
    进而把所有未上架的作品当成已上架。方向正好反了。
    """
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}"
        f"/tables/{TABLE_ID}/fields?page_size=200"
    )
    res = request_json(url, token=token)
    if res.get("code") != 0:
        log("警告: 读取字段列表失败，退回按记录值推断：" + json.dumps(res, ensure_ascii=False)[:200])
        return set()
    return {str(f.get("field_name")) for f in (res.get("data") or {}).get("items") or []}


# ---------------------------------------------------------------- 组装输出

def build(records: list[dict[str, Any]], urls: dict[str, str],
          field_names: set[str] | None = None) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str], int]:
    fields_list = [r.get("fields", {}) for r in records]

    # 优先信"字段接口"给的真实列名；取不到才退回按记录值推断
    if field_names:
        shelf_col = any(n in field_names for n in FIELD_CANDIDATES["shelf"])
    else:
        shelf_col = has_column(fields_list, "shelf")
    if not shelf_col:
        log("提醒: 表格里没有找到「上架」列，本次全部作品都视为上架。")
        log("      想用单条开关，请在表格里加一列勾选框，字段名写「上架」。")

    items: list[dict[str, Any]] = []
    video_map: dict[str, str] = {}
    cover_map: dict[str, str] = {}
    hidden = 0

    for idx, rec in enumerate(records):
        f = rec.get("fields", {})

        # --- 开关 2：单条作品是否上架 ---
        if not parse_shelf(pick(f, "shelf"), shelf_col):
            hidden += 1
            continue

        v_token = first_file_token(pick(f, "video"))
        if not v_token:
            continue
        v_url = urls.get(v_token)
        if not v_url:
            log(f"跳过（链接未解析到）：{text_of(pick(f, 'title')) or '第 ' + str(idx + 1) + ' 条'}")
            continue

        c_token = first_file_token(pick(f, "cover"))
        if c_token and urls.get(c_token):
            cover_map[c_token] = urls[c_token]
        video_map[v_token] = v_url

        items.append({
            "record_id": rec.get("record_id", ""),
            "title": text_of(pick(f, "title")) or f"作品 {idx + 1}",
            "order": to_order(pick(f, "order")),
            "categories": list_of_text(pick(f, "category")),
            "duration": text_of(pick(f, "duration")),
            "tools": text_of(pick(f, "tools")),
            "summary": text_of(f.get("简介") or f.get("说明") or f.get("summary") or ""),
            "on_shelf": True,
            "video_token": v_token,
            "video_url": v_url,
            "cover_token": c_token or "",
            "cover_url": urls.get(c_token, "") if c_token else "",
        })

    items.sort(key=lambda x: (x["order"], x["title"]))
    return items, video_map, cover_map, hidden


def save(items, video_map, cover_map, hidden, *, closed: bool = False) -> None:
    API_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_iso()

    if closed:
        # 关站：只留一个"维护中"标记，作品数据一个字都不写
        payload = {"_refreshed_at": stamp, "_site_open": False, "_count": 0,
                   "_hidden_count": 0, "_stats": {}, "items": []}
        PORTFOLIO_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        VIDEOS_FILE.write_text(json.dumps({"_refreshed_at": stamp, "_count": 0}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        COVERS_FILE.write_text(json.dumps({"_refreshed_at": stamp, "_count": 0}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log("网站开关为关闭：已输出维护中数据，作品内容未写入。")
        return

    stats: dict[str, int] = {}
    for it in items:
        for c in (it["categories"] or ["未分类"]):
            stats[c] = stats.get(c, 0) + 1

    portfolio = {
        "_refreshed_at": stamp,
        "_site_open": True,
        "_count": len(items),
        "_hidden_count": hidden,
        "_stats": dict(sorted(stats.items())),
        "items": items,
    }
    videos = dict(video_map)
    videos["_refreshed_at"] = stamp
    videos["_count"] = len(video_map)
    covers = dict(cover_map)
    covers["_refreshed_at"] = stamp
    covers["_count"] = len(cover_map)

    PORTFOLIO_FILE.write_text(json.dumps(portfolio, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    VIDEOS_FILE.write_text(json.dumps(videos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    COVERS_FILE.write_text(json.dumps(covers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log(f"上架作品 {len(items)} 条，未上架跳过 {hidden} 条，视频链接 {len(video_map)} 个，封面 {len(cover_map)} 个。")


# ---------------------------------------------------------------- 自检

def selftest() -> None:
    """不连网络，用假数据验证解析与输出逻辑。"""
    log("== 自检模式：不连飞书，只验证解析与输出 ==")

    def rec(rid, title, shelf, order=1):
        return {"record_id": rid, "fields": {
            "标题": title, "上架": shelf, "序号": order,
            "类型": ["AI 短剧"], "时长": "01:00", "AI工具": "Seedance",
            "样片": [{"file_token": "tok_" + rid}],
        }}

    fake = [rec("r1", "上架的作品", True, 1),
            rec("r2", "未上架的作品", False, 2),
            rec("r3", "空值的作品", None, 3)]
    urls = {f"tok_{r['record_id']}": f"https://example.invalid/{r['record_id']}.mp4" for r in fake}

    items, vmap, cmap, hidden = build(fake, urls)
    assert [i["title"] for i in items] == ["上架的作品"], f"上架过滤不对：{[i['title'] for i in items]}"
    assert hidden == 2, f"未上架计数不对：{hidden}"
    assert len(vmap) == 1 and len(cmap) == 0, "链接映射不对"
    log("  [通过] 上架过滤：只保留勾选的，空值视为未上架")

    no_col = [{"record_id": "x1", "fields": {"标题": "没这列", "样片": [{"file_token": "t1"}]}}]
    its2, _, _, h2 = build(no_col, {"t1": "https://example.invalid/x.mp4"})
    assert len(its2) == 1 and h2 == 0, "无「上架」列时应全部上架"
    log("  [通过] 无「上架」列时全部视为上架")

    # 回归测试：飞书会省略空字段。整列都没勾时记录里看不到"上架"这个 key，
    # 必须仍按"有这一列且未勾选"处理（全部不上架），不能误判成"没有这一列"。
    empties = [{"record_id": "e1", "fields": {"标题": "没勾选", "样片": [{"file_token": "te1"}]}}]
    its3, _, _, h3 = build(empties, {"te1": "https://example.invalid/e.mp4"},
                           {"标题", "上架", "样片", "类型"})
    assert len(its3) == 0 and h3 == 1, f"空勾选列未正确过滤：{its3} / {h3}"
    log("  [通过] 「上架」整列都没勾时按未上架处理（不再误判为没有该列）")

    assert parse_shelf("否", True) is False and parse_shelf("是", True) is True
    assert parse_shelf(True, True) is True and parse_shelf(False, True) is False
    assert parse_shelf(None, True) is False
    log("  [通过] 上架字段各种取值解析")

    log("== 自检全部通过 ==")


# ---------------------------------------------------------------- 主流程

def main() -> None:
    if "--selftest" in sys.argv:
        selftest()
        return

    missing = [n for n, v in (("LARK_APP_ID", APP_ID), ("LARK_APP_SECRET", APP_SECRET),
                             ("LARK_BASE_TOKEN", BASE_TOKEN), ("LARK_TABLE_ID", TABLE_ID)) if not v]
    if missing:
        die("缺少环境变量：" + ", ".join(missing) + "（应在仓库 Secrets 里配置）")

    if not SITE_OPEN:
        save([], {}, {}, 0, closed=True)
        return

    token = tenant_token()
    records = fetch_records(token)
    log(f"读到 {len(records)} 条记录。")
    if not records:
        log("提醒: 记录数为 0。最常见的原因是飞书应用没有被加为该多维表格的协作者。")

    fields_list = [r.get("fields", {}) for r in records]
    tokens: list[str] = []
    for f in fields_list:
        for key in ("video", "cover"):
            t = first_file_token(pick(f, key))
            if t:
                tokens.append(t)

    urls = resolve_urls(token, tokens)
    log(f"解析到 {len(urls)} 个媒体链接。")

    field_names = fetch_field_names(token)
    log(f"表格共 {len(field_names)} 列。" if field_names else "列名未取到，退回按记录值推断。")

    items, vmap, cmap, hidden = build(records, urls, field_names)
    save(items, vmap, cmap, hidden)


if __name__ == "__main__":
    main()
