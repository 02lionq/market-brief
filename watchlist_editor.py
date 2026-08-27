# -*- coding: utf-8 -*-
"""관심종목 편집기.

watchlist.json 을 직접 열어 고치는 대신 창에서 버튼으로 종목을 넣고 뺀다.

왜 브리핑 HTML 안의 버튼이 아니라 별도 창인가:
브리핑은 완성된 정적 파일이라 브라우저에서 아무리 눌러도 PC 의 파일을
고칠 수 없다. 저장을 하려면 이렇게 PC 에서 도는 프로그램이어야 한다.
"""
from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

BASE = Path(__file__).resolve().parent
WATCHLIST_FILE = BASE / "watchlist.json"
FONT = ("Malgun Gothic", 10)
FONT_B = ("Malgun Gothic", 10, "bold")
FONT_T = ("Malgun Gothic", 14, "bold")


# ── 목록 파일 읽고 쓰기 ─────────────────────────────────────
# 관심종목은 config.py 가 아니라 watchlist.json 에 둔다.
# 이 파일은 깃허브에 올라가지 않으므로, 저장소를 공개해도
# 내가 어떤 종목을 보는지는 드러나지 않는다.
def load_watchlist() -> list[tuple[str, str]]:
    """watchlist.json 을 적어둔 순서 그대로 읽는다. 없으면 빈 목록."""
    try:
        data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        raise RuntimeError(f"watchlist.json 을 읽지 못했습니다: {e}") from e
    return [(str(k), str(v)) for k, v in data.items()]


def save_watchlist(items: list[tuple[str, str]]) -> None:
    """watchlist.json 을 통째로 다시 쓴다."""
    data = {t: n for t, n in items}
    text = json.dumps(data, ensure_ascii=False, indent=2)
    WATCHLIST_FILE.write_text(text + "\n", encoding="utf-8")


# ── 창 ─────────────────────────────────────────────────────
class Editor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("관심종목 편집")
        self.minsize(520, 420)
        self.items: list[list[str]] = [list(x) for x in load_watchlist()]
        self.found: list[tuple[str, str]] = []
        self._build()
        self._refresh()
        self._fit()

    def _fit(self):
        """내용에 맞춰 창 크기를 잡는다.

        화면 배율(125%·150%)이 높으면 글자가 커져서 고정 크기로는 아래가 잘린다.
        실제로 필요한 높이를 재서 잡되, 화면을 넘지 않게 제한한다.
        """
        self.update_idletasks()
        w = max(600, self.winfo_reqwidth() + 24)
        h = self.winfo_reqheight() + 24
        w = min(w, int(self.winfo_screenwidth() * 0.9))
        h = min(h, int(self.winfo_screenheight() * 0.85))
        x = (self.winfo_screenwidth() - w) // 2
        y = max(0, (self.winfo_screenheight() - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ---------- 화면 구성 ----------
    def _build(self):
        pad = dict(padx=14)

        tk.Label(self, text="관심종목 편집", font=FONT_T,
                 anchor="w").pack(fill="x", pady=(14, 2), **pad)
        tk.Label(self, text="브리핑의 '관심종목' 표에 나올 종목을 정합니다.",
                 font=FONT, fg="#666", anchor="w").pack(fill="x", **pad)

        # --- 검색 ---
        box = tk.LabelFrame(self, text=" 종목 찾아서 추가 ", font=FONT_B,
                            padx=10, pady=8)
        box.pack(fill="x", pady=(12, 8), **pad)

        row = tk.Frame(box)
        row.pack(fill="x")
        tk.Label(row, text="검색", font=FONT, width=6,
                 anchor="w").pack(side="left")
        self.q = tk.Entry(row, font=FONT)
        self.q.pack(side="left", fill="x", expand=True)
        self.q.bind("<Return>", lambda _e: self.search())
        self.btn_find = tk.Button(row, text="검색", font=FONT, width=8,
                                  command=self.search)
        self.btn_find.pack(side="left", padx=(6, 0))
        tk.Label(box, text="티커(NVDA) 또는 영문 회사명(apple)으로 검색하세요.",
                 font=("Malgun Gothic", 9), fg="#888",
                 anchor="w").pack(fill="x", pady=(3, 6))

        self.results = tk.Listbox(box, font=FONT, height=4,
                                  activestyle="none", exportselection=False)
        self.results.pack(fill="x")
        self.results.bind("<<ListboxSelect>>", self._pick)

        row2 = tk.Frame(box)
        row2.pack(fill="x", pady=(8, 2))
        tk.Label(row2, text="표시 이름", font=FONT, width=8,
                 anchor="w").pack(side="left")
        self.name = tk.Entry(row2, font=FONT)
        self.name.pack(side="left", fill="x", expand=True)
        tk.Button(row2, text="+ 추가", font=FONT_B, width=8,
                  command=self.add).pack(side="left", padx=(6, 0))
        tk.Label(box, text="표시 이름은 한글로 바꿔도 됩니다. 브리핑 표에 그대로 나옵니다.",
                 font=("Malgun Gothic", 9), fg="#888",
                 anchor="w").pack(fill="x", pady=(3, 0))

        # --- 저장 (목록보다 먼저 '아래'에 붙인다) ---
        # 이렇게 해야 창을 아무리 줄여도 저장 버튼이 화면 밖으로 밀려나지 않는다.
        # 나중에 pack 하면 늘어나는 목록에 밀려 잘린다.
        bar = tk.Frame(self)
        bar.pack(side="bottom", fill="x", pady=(6, 14), **pad)
        self.status = tk.Label(bar, text="", font=FONT, fg="#0a7",
                               anchor="w", wraplength=520, justify="left")
        self.status.pack(fill="x", pady=(0, 6))
        btns = tk.Frame(bar)
        btns.pack(fill="x")
        tk.Button(btns, text="저장", font=FONT_B, width=10,
                  command=self.save).pack(side="left")
        self.btn_save_run = tk.Button(
            btns, text="저장하고 브리핑 새로 만들기", font=FONT,
            command=self.save_and_run)
        self.btn_save_run.pack(side="left", padx=6)
        tk.Button(btns, text="닫기", font=FONT, width=8,
                  command=self.destroy).pack(side="right")

        # --- 현재 목록 (남는 공간을 모두 차지) ---
        cur = tk.LabelFrame(self, text=" 현재 관심종목 ", font=FONT_B,
                            padx=10, pady=8)
        cur.pack(fill="both", expand=True, pady=(4, 0), **pad)

        inner = tk.Frame(cur)
        inner.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(inner, font=FONT, activestyle="none",
                                  exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(inner, orient="vertical",
                           command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.config(yscrollcommand=sb.set)

        side = tk.Frame(cur)
        side.pack(fill="x", pady=(8, 0))
        for txt, fn in (("▲ 위로", self.up), ("▼ 아래로", self.down),
                        ("선택 삭제", self.remove)):
            tk.Button(side, text=txt, font=FONT, width=10,
                      command=fn).pack(side="left", padx=(0, 6))
        self.count = tk.Label(side, text="", font=FONT, fg="#666")
        self.count.pack(side="right")

    # ---------- 동작 ----------
    def _refresh(self):
        self.listbox.delete(0, "end")
        for t, n in self.items:
            self.listbox.insert("end", f"  {t:<7}  {n}")
        self.count.config(text=f"{len(self.items)}개")

    def _say(self, msg, ok=True):
        self.status.config(text=msg, fg="#0a7" if ok else "#c33")

    def search(self):
        q = self.q.get().strip()
        if not q:
            return
        self.btn_find.config(state="disabled", text="검색 중")
        self.results.delete(0, "end")
        self._say("검색 중…")

        def work():
            try:
                import yfinance as yf
                quotes = yf.Search(q, max_results=8).quotes or []
                found = []
                for x in quotes:
                    sym = x.get("symbol")
                    nm = x.get("shortname") or x.get("longname") or ""
                    if sym:
                        found.append((sym, nm))
            except Exception as e:
                found, err = [], f"{type(e).__name__}"
                self.after(0, lambda: self._say(f"검색 실패: {err}", False))
            self.after(0, lambda: self._show(found))

        threading.Thread(target=work, daemon=True).start()

    def _show(self, found):
        self.found = found
        self.btn_find.config(state="normal", text="검색")
        self.results.delete(0, "end")
        for sym, nm in found:
            self.results.insert("end", f"  {sym:<10}  {nm}")
        if found:
            self.results.selection_set(0)
            self._pick()
            self._say(f"{len(found)}개 찾음. 목록에서 고르세요.")
        else:
            self._say("결과가 없습니다. 영문 회사명이나 티커로 다시 검색해 보세요.", False)

    def _pick(self, _e=None):
        sel = self.results.curselection()
        if not sel or sel[0] >= len(self.found):
            return
        sym, nm = self.found[sel[0]]
        self.name.delete(0, "end")
        self.name.insert(0, nm)
        self._say(f"{sym} 선택됨. 표시 이름을 다듬고 '+ 추가'를 누르세요.")

    def add(self):
        sel = self.results.curselection()
        if not sel or sel[0] >= len(self.found):
            self._say("먼저 검색해서 종목을 고르세요.", False)
            return
        sym = self.found[sel[0]][0]
        nm = self.name.get().strip() or sym
        if any(t == sym for t, _ in self.items):
            self._say(f"{sym} 은(는) 이미 목록에 있습니다.", False)
            return
        self.items.append([sym, nm])
        self._refresh()
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set("end")
        self.listbox.see("end")
        self._say(f"{sym} 추가됨. 저장을 눌러야 실제로 반영됩니다.")

    def _sel(self):
        s = self.listbox.curselection()
        return s[0] if s else None

    def remove(self):
        i = self._sel()
        if i is None:
            self._say("삭제할 종목을 목록에서 고르세요.", False)
            return
        t, _ = self.items.pop(i)
        self._refresh()
        self._say(f"{t} 삭제됨. 저장을 눌러야 실제로 반영됩니다.")

    def _move(self, delta):
        i = self._sel()
        if i is None:
            return
        j = i + delta
        if not (0 <= j < len(self.items)):
            return
        self.items[i], self.items[j] = self.items[j], self.items[i]
        self._refresh()
        self.listbox.selection_set(j)

    def up(self):
        self._move(-1)

    def down(self):
        self._move(1)

    def save(self) -> bool:
        if not self.items:
            if not messagebox.askyesno(
                    "확인", "관심종목이 하나도 없습니다.\n"
                            "이대로 저장하면 브리핑에서 관심종목 표가 사라집니다.\n"
                            "저장할까요?"):
                return False
        try:
            save_watchlist([(t, n) for t, n in self.items])
        except Exception as e:
            messagebox.showerror("저장 실패", f"{type(e).__name__}: {e}")
            return False
        self._say(f"저장 완료 — {len(self.items)}개 종목")
        return True

    def save_and_run(self):
        if not self.save():
            return
        self.btn_save_run.config(state="disabled", text="만드는 중…")
        self._say("브리핑을 새로 만들고 있습니다. 20초쯤 걸립니다…")

        def work():
            import subprocess
            exe = BASE / ".venv" / "Scripts" / "python.exe"
            try:
                r = subprocess.run([str(exe), str(BASE / "brief.py")],
                                   cwd=str(BASE), capture_output=True,
                                   text=True, timeout=600)
                ok = r.returncode == 0
                msg = ("브리핑을 새로 만들었습니다. 브라우저를 확인하세요."
                       if ok else "브리핑 생성 실패 — last-run.log 를 확인하세요.")
            except Exception as e:
                ok, msg = False, f"실행 실패: {type(e).__name__}"
            self.after(0, lambda: (self._say(msg, ok),
                                   self.btn_save_run.config(
                                       state="normal",
                                       text="저장하고 브리핑 새로 만들기")))

        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    Editor().mainloop()
