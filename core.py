# -*- coding: utf-8 -*-
"""
战争雷霆 游戏内聊天实时翻译 · 核心
采集：http://127.0.0.1:8111/gamechat   翻译：OpenAI 兼容云端端点

这一层不含任何界面代码，可单独用 --console 跑通后再开窗口。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path

import requests
import yaml

WT_CHAT_URL = "http://127.0.0.1:8111/gamechat"

# ── 字符清洗表 ──────────────────────────────────────────────────────
# 实测：接口返回的每个汉字之间都嵌了 U+200B，不剥掉则「友军」≠「友军」，
# 词表匹配、CJK 判定、mode 映射全部失效。
_ZERO_WIDTH = {cp: None for cp in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x180E)}
_BIDI = {cp: None for cp in (0x200E, 0x200F, 0x202A, 0x202B, 0x202C,
                             0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)}
_TAG_RE = re.compile(r"<[^<>]{1,60}>")          # 游戏内富文本 <color=#FF96966E>
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u30FF\uAC00-\uD7AF]")
_NOISE_RE = re.compile(r"^[\W_]+$")
# 纯笑声/表情：不必翻译（lol/lmao 不在此列，它们归词表处理）
_LAUGH_RE = re.compile(r"^(?:[xX]+[dD]+|[hH]{3,}|[)）]{2,}|[\^]{2,})$")

# 实测：mode 是本地化字符串，不是 All/Team 枚举
MODE_MAP = {"友军": "Team", "所有人": "All", "小队": "Squad", "战队": "Squad",
            "team": "Team", "all": "All", "squad": "Squad"}

# 俄区玩家用西里尔字母冒充拉丁字母（а е о с р х у 与 a e o c p x y 同形）
HOMOGLYPH = str.maketrans({
    "а": "a", "е": "e", "о": "o", "с": "c", "р": "p", "х": "x", "у": "y",
    "А": "A", "Е": "E", "О": "O", "С": "C", "Р": "P", "Х": "X", "У": "Y",
    "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g",
    "ο": "o", "α": "a", "ν": "v", "ι": "i", "κ": "k", "ρ": "p", "τ": "t",
})


# ══════════════════════════════════════════════════════════════════
# 归一化
# ══════════════════════════════════════════════════════════════════
def normalize(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    s = unicodedata.normalize("NFKC", raw)      # 全角→半角
    s = s.translate(_ZERO_WIDTH).translate(_BIDI)
    s = _TAG_RE.sub("", s)                      # 去掉 <color=...>
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"[ \t\u00a0]+", " ", s).strip()
    return s[:200]


def restore_homoglyph(s: str) -> str:
    """只有当整句以拉丁字母为主时才还原同形字；真正的俄语原文交给模型翻。"""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return s
    ascii_ratio = sum(1 for c in letters if c.isascii()) / len(letters)
    return s.translate(HOMOGLYPH) if ascii_ratio >= 0.6 else s


def map_mode(raw_mode: str) -> str:
    key = str(raw_mode or "").translate(_ZERO_WIDTH).strip()
    return MODE_MAP.get(key.lower(), MODE_MAP.get(key, key or "All"))


def is_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s))


def should_skip(text: str) -> bool:
    """返回 True 表示这条不需要翻译。注意：必须在词表替换之前调用。"""
    t = text.strip()
    if not t:
        return True
    if is_cjk(t):                       # 已经是中文（含游戏自带的战术标记播报）
        return True
    if _NOISE_RE.match(t):              # 纯标点 / 纯表情符号
        return True
    if _LAUGH_RE.match(t):              # xD / hahaha / ))) / ^^
        return True
    if len(re.sub(r"[^\w]", "", t, flags=re.UNICODE)) <= 1:   # "+1"、"6"、"D"
        return True
    return False


# ══════════════════════════════════════════════════════════════════
# 词表
# ══════════════════════════════════════════════════════════════════
class Glossary:
    def __init__(self, path: Path):
        data = {}
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            pass
        self.map: dict[str, str] = {k.strip().lower(): v
                                    for k, v in data.items()
                                    if isinstance(k, str) and isinstance(v, str)}
        if self.map:
            keys = sorted(self.map, key=len, reverse=True)
            body = "|".join(re.escape(k) for k in keys)
            self._re = re.compile(rf"(?<![a-z0-9])({body})(?![a-z0-9])", re.IGNORECASE)
        else:
            self._re = None

    def apply(self, text: str) -> tuple[str, bool]:
        """返回 (替换后的文本, 是否整句都被词表覆盖)"""
        if not self._re:
            return text, False
        out = self._re.sub(lambda m: self.map.get(m.group(0).lower(), m.group(0)), text)
        tokens = re.findall(r"[a-z0-9']+", text.lower())
        covered = bool(tokens) and all(t in self.map for t in tokens)
        return out, covered


# ══════════════════════════════════════════════════════════════════
# 翻译
# ══════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = (
    "你是《战争雷霆》游戏内聊天的翻译器，把玩家消息译成简体中文。\n"
    "要求：\n"
    "- 口语、简短，长度不超过原文两倍\n"
    "- 保留玩家 ID、载具名、地图点位（如 C4）原文，不翻译\n"
    "- 缩写按游戏语境理解：nt=差一点，gg=打得不错，wp=干得好\n"
    "- 只输出译文本身，不要解释、不要引号、不要任何前缀\n"
    "- 输入是编号多行时，输出必须等量编号，严格格式：序号|译文"
)

_NUM_RE = re.compile(r"^\s*(?:[-*•]\s*)?(\d{1,3})\s*[.、|｜:：)\]]\s*(.*)$")
_PREFIX_RE = re.compile(r'^\s*(?:译文|翻译)\s*[:：]\s*')


def _clean(s: str) -> str:
    s = _PREFIX_RE.sub("", s).strip()
    s = s.strip('"“”\'‘’「」《》')
    return s.replace("\n", " ").strip()


def _parse_numbered(out: str, n: int) -> list[str] | None:
    got: dict[int, str] = {}
    for line in out.splitlines():
        m = _NUM_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        if 1 <= idx <= n and idx not in got:
            v = _clean(m.group(2))
            if v:
                got[idx] = v
    if len(got) == n:
        return [got[i + 1] for i in range(n)]
    if n == 1:
        single = _clean(out)
        return [single] if single else None
    return None


ZH2EN_PROMPT = (
    "你在玩《战争雷霆》，需要把一句话意思用英文发到游戏聊天里。\n"
    "要求：\n"
    "- 地道、口语、简短，像欧美玩家随手打的，不要书面语\n"
    "- 不保留中文，全部用英文\n"
    "- 玩家 ID、载具名照抄\n"
    "- 只输出这句话本身，不要解释、不要引号、不要前缀\n"
)


class Translator:
    def __init__(self, cfg: dict):
        self.cfg = cfg["llm"]
        self.privacy = cfg.get("privacy", {})
        self.cache_size = int(cfg.get("queue", {}).get("cache_size", 2000))
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.trust_env = False          # 绕开系统代理，别让 127.0.0.1 被劫
        self.stats = {"req": 0, "line": 0, "hit": 0, "err": 0}

    # ── 通道 ──
    def _providers(self) -> list[dict]:
        provs = [self.cfg]
        fb = self.cfg.get("fallback") or {}
        if fb.get("enabled") and fb.get("base_url") and fb.get("model"):
            provs.append(fb)
        return provs

    def _zh2en_providers(self) -> list[dict]:
        """中译英优先走 llm.zh2en（FLASH 模型、关掉思维链），失败回落主通道。"""
        out: list[dict] = []
        z = self.cfg.get("zh2en") or {}
        if z.get("enabled") and z.get("base_url") and z.get("model"):
            out.append(z)
        out.extend(self._providers())
        return out

    @staticmethod
    def _key_of(prov: dict) -> str:
        return (prov.get("api_key") or "").strip() or \
               os.environ.get(prov.get("api_key_env") or "", "").strip()

    def _post(self, prov: dict, user_content: str, n: int,
              system: str = SYSTEM_PROMPT) -> str:
        key = self._key_of(prov)
        if not key:
            raise RuntimeError(f"未配置 API 密钥（{prov.get('api_key_env') or 'api_key'}）")
        url = prov["base_url"].rstrip("/") + "/chat/completions"
        payload = {
            "model": prov["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user_content}],
            "temperature": float(prov.get("temperature",
                                          self.cfg.get("temperature", 0.1))),
            "max_tokens": int(prov.get("max_tokens") or min(1024, 96 * n + 64)),
            "stream": False,
        }
        # 思考强度：各家写法不同，按 provider 配置选择性拼装
        if prov.get("enable_thinking") is not None:
            payload["enable_thinking"] = bool(prov["enable_thinking"])
        if prov.get("reasoning_effort"):
            payload["reasoning_effort"] = prov["reasoning_effort"]
        if isinstance(prov.get("extra"), dict):
            payload.update(prov["extra"])

        last = None
        for attempt in range(int(self.cfg.get("retries", 2)) + 1):
            try:
                r = self._session.post(
                    url, json=payload,
                    timeout=float(prov.get("timeout", self.cfg.get("timeout", 8))),
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"})
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code}")
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
                data = json.loads(r.content.decode("utf-8"))
                return data["choices"][0]["message"]["content"]
            except Exception as e:                    # noqa: BLE001
                last = e
                if attempt < int(self.cfg.get("retries", 2)):
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(str(last))

    # ── 缓存 ──
    def cache_get(self, text: str) -> str | None:
        with self._lock:
            v = self._cache.get(text.lower())
            if v is not None:
                self._cache.move_to_end(text.lower())
                self.stats["hit"] += 1
            return v

    def cache_put(self, text: str, value: str) -> None:
        with self._lock:
            self._cache[text.lower()] = value
            self._cache.move_to_end(text.lower())
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    # ── 主入口 ──
    def translate_batch(self, texts: list[str]) -> list[str]:
        results: list[str | None] = [None] * len(texts)
        todo: list[int] = []
        for i, t in enumerate(texts):
            hit = self.cache_get(t)
            if hit is not None:
                results[i] = hit
            else:
                todo.append(i)
        if not todo:
            return [r or "" for r in results]

        if not self.privacy.get("allow_cloud", True):
            for i in todo:
                results[i] = texts[i]
            return [r or "" for r in results]

        batch = [texts[i] for i in todo]
        outs = self._call(batch)
        for i, o in zip(todo, outs):
            val = o or texts[i]
            results[i] = val
            if o:
                self.cache_put(texts[i], val)
        return [r or "" for r in results]

    def _call(self, batch: list[str]) -> list[str | None]:
        user = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(batch))
        for prov in self._providers():
            try:
                self.stats["req"] += 1
                out = self._post(prov, user, len(batch))
                parsed = _parse_numbered(out, len(batch))
                if parsed:
                    self.stats["line"] += len(batch)
                    return parsed
                if len(batch) > 1:                      # 编号乱套，退回逐条
                    return [self._single(t) for t in batch]
                return [None]
            except Exception:                            # noqa: BLE001
                self.stats["err"] += 1
                continue
        return [None] * len(batch)

    def _single(self, text: str) -> str | None:
        for prov in self._providers():
            try:
                out = self._post(prov, f"1. {text}", 1)
                parsed = _parse_numbered(out, 1)
                return parsed[0] if parsed else None
            except Exception:                            # noqa: BLE001
                continue
        return None

    def warmup(self) -> None:
        """预热 DNS/TLS 连接与模型。实测不预热时首条要付 1.7s（主通道）/ 8s（新端点）。"""
        z = self._zh2en_providers()
        if z:
            try:
                self._post(z[0], "你好", 1, system=ZH2EN_PROMPT)
            except Exception:                            # noqa: BLE001
                pass
        try:
            self._post(self._providers()[0], "1. gg", 1)
        except Exception:                                # noqa: BLE001
            pass

    def to_english(self, text: str) -> str | None:
        """中文 → 英文，用于替玩家发送。失败返回 None。

        优先用 llm.zh2en 单独的通道（FLASH + 关思考），失败再回落到主通道。
        """
        text = text.strip()
        if not text:
            return None
        errors = []
        for prov in self._zh2en_providers():
            try:
                out = self._post(prov, text, 1, system=ZH2EN_PROMPT)
            except Exception as e:                       # noqa: BLE001
                errors.append(str(e))
                continue
            line = _clean(out.split("\n")[0])
            # 模型偶尔会带上中文注解，截到第一个换行/句点为止
            if line:
                return line[:200]
        if errors:
            raise RuntimeError(errors[-1])
        return None


# ══════════════════════════════════════════════════════════════════
# 对局保活：在对局中时定期预热翻译通道，避免临时发言时撞冷启动
# ══════════════════════════════════════════════════════════════════
WT_STATE_URL = "http://127.0.0.1:8111/state"


class KeepAlive(threading.Thread):
    """检测对局状态（/state 的 valid 字段，机库为 false），
    进入对局立即预热一次，之后每 interval 秒 ping 一次 zh2en 通道；
    不在对局则完全静默，不产生任何云端请求。"""

    def __init__(self, translator: "Translator", cfg: dict, on_log=print):
        super().__init__(daemon=True, name="keepalive")
        self.tr = translator
        self.cfg = cfg
        self.on_log = on_log
        self.stop_evt = threading.Event()
        self.interval = float(cfg.get("interval", 90))
        self.check = float(cfg.get("check", 5))
        self.pings = 0
        self.in_battle = False
        self._sess = requests.Session()
        self._sess.trust_env = False

    def _battle_now(self) -> bool:
        try:
            r = self._sess.get(WT_STATE_URL, timeout=1.5)
            return bool(json.loads(r.content.decode("utf-8")).get("valid"))
        except Exception:                  # noqa: BLE001
            return False

    def _ping(self) -> bool:
        provs = self.tr._zh2en_providers()
        if not provs:
            return False
        try:
            self.tr._post(provs[0], "ok", 1, system=ZH2EN_PROMPT)
            self.pings += 1
            return True
        except Exception:                  # noqa: BLE001
            return False

    def run(self) -> None:
        last_ping = 0.0
        was = False
        while not self.stop_evt.is_set():
            now_battle = self._battle_now()
            if now_battle and not was:
                self.on_log("[保活] 进入对局，预热翻译通道")
                self._ping()
                last_ping = time.monotonic()
            elif not now_battle and was:
                self.on_log("[保活] 离开对局，暂停（不再产生云端请求）")
            elif now_battle and time.monotonic() - last_ping >= self.interval:
                if self._ping():
                    last_ping = time.monotonic()
                else:
                    last_ping = time.monotonic() - self.interval + 15  # 失败 15s 后重试
            was = now_battle
            self.in_battle = now_battle
            self.stop_evt.wait(self.check)

    def stop(self) -> None:
        self.stop_evt.set()


# ══════════════════════════════════════════════════════════════════
# 采集
# ══════════════════════════════════════════════════════════════════
class Poller(threading.Thread):
    """
    lastId 语义（实测）：严格大于过滤。

    ⚠️ id 是「本局会话」内的计数器，游戏重启后会从头计数。
    因此绝不能盲目持久化 last_id：一旦残留一个较大的旧值，
    接口会永远返回空数组——不报错、不崩溃，只是静默失效。
    策略：启动时用 lastId=0 探一次把游标对齐到当前最大 id（只回放最后几条），
    运行中若长时间无消息也探一次，发现计数器回退就归零重来。
    """

    def __init__(self, cfg: dict, out_queue, state_path: Path, on_log=print):
        super().__init__(daemon=True, name="poller")
        self.cfg = cfg["poll"]
        self.q = out_queue
        self.state_path = state_path
        self.on_log = on_log
        self.stop_evt = threading.Event()
        self.last_id = 0
        self._sess = requests.Session()
        self._sess.trust_env = False
        self._last_save = 0.0
        self._silent_since = time.monotonic()
        self.ok = False
        self.skipped = 0
        self.dropped = 0            # 队列满被挤掉的最旧消息数

    # ── 状态 ──
    def _load_state(self) -> None:
        try:
            self.last_id = int(json.loads(self.state_path.read_text("utf-8"))["last_id"])
        except Exception:                        # noqa: BLE001
            self.last_id = 0

    def _save_state(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_save < 1.0:
            return
        self._last_save = now
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_id": self.last_id}, ensure_ascii=False), "utf-8")
            tmp.replace(self.state_path)
        except Exception:                        # noqa: BLE001
            pass

    # ── HTTP ──
    def _fetch(self, last_id: int) -> list[dict]:
        r = self._sess.get(WT_CHAT_URL, params={"lastId": last_id},
                           timeout=float(self.cfg.get("http_timeout", 0.8)))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        # 响应头只有 application/json，没有 charset；走 r.text 会被猜成 GBK
        data = json.loads(r.content.decode("utf-8"))
        if not isinstance(data, list):
            raise RuntimeError("响应不是数组")
        return data

    def _probe(self, replay: bool = True) -> bool:
        """用 lastId=0 探当前会话的最大 id，把游标对齐到最新。

        游戏重启后 id 会从头计数；若残留一个较大的旧游标，
        接口会永远返回空数组——不报错、不崩溃，只是静默失效。

        replay 只在「首次运行」或「检测到 id 回退」时为 True。
        普通重启时不能重放——那会把已经看过的旧消息又放一遍。
        """
        data = self._fetch(0)
        self.ok = True
        newest = max((int(m["id"]) for m in data), default=0)
        reset = newest < self.last_id
        self.last_id = newest
        if reset or replay:
            for m in data[-3:]:            # 只回放最后 3 条，不刷屏
                self._emit(m)
        self._save_state(force=True)
        return True

    # ── 分发 ──
    def _emit(self, m: dict) -> None:
        msg = normalize(m.get("msg", ""))
        if should_skip(msg):
            self.skipped += 1
            return
        try:
            self.q.put_nowait({
                "id": int(m.get("id", 0)),
                "sender": normalize(str(m.get("sender", "")))[:32] or "?",
                "src": msg,
                "enemy": bool(m.get("enemy", False)),
                "mode": map_mode(m.get("mode", "")),
                "time": m.get("time", 0),
                "_t0": time.monotonic(),
            })
        except Exception:                  # noqa: BLE001  队列满，丢最旧
            self.dropped += 1
            try:
                self.q.get_nowait()
                self.q.put_nowait({
                    "id": int(m.get("id", 0)),
                    "sender": normalize(str(m.get("sender", "")))[:32] or "?",
                    "src": msg, "enemy": bool(m.get("enemy", False)),
                    "mode": map_mode(m.get("mode", "")), "time": m.get("time", 0),
                    "_t0": time.monotonic(),
                })
            except Exception:              # noqa: BLE001
                pass

    # ── 主循环 ──
    def run(self) -> None:
        # 先判断这是不是首次运行：决定开机探测要不要回放最后 3 条
        first_run = not self.state_path.exists()
        self._load_state()
        try:
            self._probe(replay=first_run)
        except Exception:                  # noqa: BLE001
            self.ok = False

        while not self.stop_evt.is_set():
            try:
                data = self._fetch(self.last_id)
                self.ok = True
                # 不依赖服务器返回顺序：先筛后排序，避免乱序时静默丢消息
                pending = [m for m in data if int(m.get("id", 0)) > self.last_id]
                if pending:
                    pending.sort(key=lambda m: int(m["id"]))
                    self._silent_since = time.monotonic()
                    for m in pending:
                        mid = int(m["id"])
                        if mid <= self.last_id:
                            continue
                        self.last_id = mid
                        self._emit(m)
                    self._save_state()
                elif time.monotonic() - self._silent_since > 15:
                    # 长时间收不到新消息：很可能是游戏重启后 id 从头计数，
                    # 旧游标过大使接口永远返回空数组。这里必须重新对齐，
                    # 否则就是“不报错、不崩溃、但什么都不再显示”。
                    try:
                        self._probe(replay=False)
                    except Exception:      # noqa: BLE001
                        pass
                    self._silent_since = time.monotonic()
                time.sleep(float(self.cfg.get("interval", 0.25)))
            except requests.RequestException:
                self.ok = False
                time.sleep(float(self.cfg.get("backoff", 1.0)))
                if time.monotonic() - self._silent_since > 15:
                    try:
                        self._probe(replay=False)
                    except Exception:      # noqa: BLE001
                        pass
                    self._silent_since = time.monotonic()
            except (ValueError, KeyError, TypeError):
                time.sleep(0.5)
            except Exception:              # noqa: BLE001
                time.sleep(1.0)

    def stop(self) -> None:
        self.stop_evt.set()
        self._save_state(force=True)      # 退出前强制落盘，否则最后 1 秒的游标会丢


# ══════════════════════════════════════════════════════════════════
# 翻译工作线程（攒批）
# ══════════════════════════════════════════════════════════════════
class TranslatorWorker(threading.Thread):
    def __init__(self, cfg: dict, in_queue, translator: Translator, glossary: Glossary,
                 on_ready, on_log=print):
        super().__init__(daemon=True, name="translator")
        self.cfg = cfg["queue"]
        self.q = in_queue
        self.tr = translator
        self.gl = glossary
        self.on_ready = on_ready
        self.on_log = on_log
        self.stop_evt = threading.Event()

    def _prepare(self, rec: dict) -> dict:
        """词表预替换；整句被词表覆盖则直接给答案，不花 token。"""
        text, covered = self.gl.apply(restore_homoglyph(rec["src"]))
        rec["prepared"] = text
        rec["glossed"] = covered
        return rec

    def _flush(self, pending: list[dict]) -> None:
        if not pending:
            return
        todo = [r for r in pending if not r.get("glossed")]
        out: list[dict] = []

        for r in pending:
            if r.get("glossed"):
                out.append({**r, "dst": r["prepared"]})
        if todo:
            texts = [r["prepared"] for r in todo]
            try:
                zh = self.tr.translate_batch(texts)
            except Exception as e:                  # noqa: BLE001
                self.on_log(f"[翻译失败] {e}")
                zh = texts
            for r, z in zip(todo, zh):
                out.append({**r, "dst": z})
        if out:
            self.on_ready(out)

    def run(self) -> None:
        pending: list[dict] = []
        max_lines = int(self.cfg.get("max_lines", 5))
        max_wait = float(self.cfg.get("max_wait", 0.35))
        grace = float(self.cfg.get("grace", 0.06))
        while not self.stop_evt.is_set():
            try:
                rec = self.q.get(timeout=0.2)
            except Exception:                       # noqa: BLE001  queue.Empty
                if pending:
                    self._flush(pending)
                    pending = []
                continue
            pending.append(self._prepare(rec))
            if len(pending) >= max_lines:
                self._flush(pending)
                pending = []
                continue
            # 空闲时几乎不攒（队列里没东西说明后面也没东西），
            # 忙碌、或同一次轮询里进了多条时才给足攒批窗口。
            window = grace if self.q.empty() else max_wait
            deadline = time.monotonic() + window
            while len(pending) < max_lines and time.monotonic() < deadline:
                try:
                    pending.append(self._prepare(self.q.get(timeout=deadline - time.monotonic())))
                except Exception:                   # noqa: BLE001
                    break
            self._flush(pending)
            pending = []
        if pending:
            self._flush(pending)

    def stop(self) -> None:
        self.stop_evt.set()


# ══════════════════════════════════════════════════════════════════
def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: Path) -> dict:
    path = Path(path)
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # 同目录下的 config.local.yaml 作为本地覆盖，不进版本库
    local = path.with_name("config.local.yaml")
    if local.exists():
        try:
            over = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
            if isinstance(over, dict):
                _deep_merge(cfg, over)
        except Exception:                # noqa: BLE001
            pass
    cfg.setdefault("llm", {})
    cfg.setdefault("poll", {})
    cfg.setdefault("queue", {})
    cfg.setdefault("ui", {})
    cfg.setdefault("privacy", {})
    cfg.setdefault("web", {})
    cfg.setdefault("compose", {})
    return cfg