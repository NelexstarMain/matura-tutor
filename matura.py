"""matura.py: biblioteka projektu (baza zadań, plan, pętla błędu, arkusze, raporty, kontrola) i jedyne wejście CLI.

Uruchamianie: python3 matura.py <polecenie> [opcje]   (polecenia: patrz README.md, sekcja CLI)
Z Pythona:    import matura as m; m.Szukaj(...)
"""
from __future__ import annotations

import argparse
import argparse, re, shutil
import importlib
import importlib.util
import json
import logging
import math
import os
import re
import unicodedata
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import warnings
from collections import Counter
from collections import Counter, defaultdict
from datetime import date, datetime
from datetime import date, datetime, timedelta
from datetime import datetime
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, Set
from zoneinfo import ZoneInfo

try:
    import numpy as np
    import scipy  # noqa: F401
except ImportError:  # świeże środowisko: doinstaluj zależności z requirements.txt i uruchom ponownie
    import subprocess as _sp
    print("Brak bibliotek, instaluję requirements.txt ...", file=sys.stderr)
    _sp.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(Path(__file__).resolve().parent / "requirements.txt")], check=False)
    import numpy as np

from scipy.ndimage import maximum_filter, minimum_filter  # noqa: E402

# Ciężkie biblioteki do składania PDF: brak nie blokuje zwykłej pracy (python3 matura.py kontrola doinstaluje)
try:
    from PIL import Image
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader, simpleSplit
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
except ImportError:  # pragma: no cover
    pass


# ==============================================================================
# SEKCJA: matura (dawniej matura.py)
# ==============================================================================

# matura.py — Biblioteka dostępu do wektorowej bazy zadań maturalnych dla Claude'a i agentów AI.
#
# Wszystkie dane (wektory 160D, treści LaTeX, zdjęcia, 160 tagów, status rozwiązań i historia)
# znajdują się w jednym pliku bazy SQLite: matura.db (tabele: main, desc, photos, done, history, cooldown_fails, dims).



logger = logging.getLogger("matura")

try:
    from scipy.special import sph_harm_y as _sph_harm_y
    def _sph_harm_calc(m: int, l: int, theta_az, phi_pol):
        # theta_az: azymut [0, 2pi], phi_pol: kąt biegunowy [0, pi]
        return _sph_harm_y(l, m, phi_pol, theta_az)
except ImportError:
    from scipy.special import sph_harm as _sph_harm_legacy
    def _sph_harm_calc(m: int, l: int, theta_az, phi_pol):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return _sph_harm_legacy(m, l, theta_az, phi_pol)

# ==============================================================================
# KONFIGURACJA I STAŁE
# ==============================================================================

BASE = Path(__file__).resolve().parent
DB = BASE / "matura.db"
DANE = BASE / "dane"          # pliki, które kod czyta (harmonogram, listy obrazów, formy rozgrzewki)
if not DB.exists() and (DANE / "baza_wzorcowa.db").exists():   # pierwsze uruchomienie: baza bez historii prób
    shutil.copy(DANE / "baza_wzorcowa.db", DB)
    print("Utworzono matura.db z dane/baza_wzorcowa.db (bez historii prób).", file=sys.stderr)
NOTATKI = BASE / "notatki"    # to, co zapisuje uczeń i sesje (dziennik błędów, postęp, fiszki)

NUM_DIMS = 160
DEFAULT_W_KAT = 1.0
MIN_RADIUS = 0.5  # minimalny promień punktu, by ocena 0 nie lądowała w początku układu
DEFAULT_W_DL = 0.20
DEFAULT_RANDOMNESS = 0.15  # lekka losowość, by zapytania nie powtarzały w kółko tego samego zadania
FAIL_COOLDOWN_DAYS = 3.0   # zadanie nieudane znika z wyszukiwania na 3 dni kwarantanny
TZ = ZoneInfo("Europe/Warsaw")  # uczeń uczy się w Polsce; kontener działa w UTC


def Dzis() -> date:
    """Dzisiejsza data w Polsce (nie w UTC kontenera)."""
    return datetime.now(TZ).date()


def _data(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, TZ)

class MaturaError(Exception):
    """Błąd biblioteki matura."""
    pass

# ==============================================================================
# SILNIK PAMIĘCI RAM I SQLITE
# ==============================================================================

class _StorageEngine:
    _instance: Optional["_StorageEngine"] = None

    def __init__(self):
        # Tablica wektorów w RAM (float32, 4608x160 to zaledwie ~2.9 MB)
        self.vectors: np.ndarray = np.empty((0, NUM_DIMS), dtype=np.float32)
        self.norms: np.ndarray = np.empty((0,), dtype=np.float32)
        self.ids: np.ndarray = np.empty((0,), dtype=np.int32)
        self.id_to_idx: Dict[int, int] = {}
        self.done_set: Set[int] = set()

        # Rzutowanie PCA 160D -> 3D
        self.pca_mean: np.ndarray = np.empty((0,), dtype=np.float32)
        self.pca_basis: np.ndarray = np.empty((0, 3), dtype=np.float32)

        # Taksonomia 160 wymiarów (z tabeli dims w matura.db)
        self.dims_list: List[Dict[str, Any]] = []
        self.tag_to_id: Dict[str, int] = {}
        self.norm_tag_to_id: Dict[str, int] = {}
        self.segments_dict: Dict[str, List[int]] = {"A": [], "B": [], "C": []}
        self.makro_to_ids: Dict[str, List[int]] = {}
        self.pca_var_ratio: float = 0.0
        self._load_from_sqlite()

    @classmethod
    def get(cls) -> "_StorageEngine":
        if cls._instance is None:
            cls._instance = _StorageEngine()
        return cls._instance

    def get_conn(self) -> sqlite3.Connection:
        if not DB.exists():
            raise MaturaError(f"Nie znaleziono bazy danych '{DB}'.")
        conn = sqlite3.connect(DB, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_from_sqlite(self):
        """Ładuje wektory z tabeli 'main', taksonomię z 'dims' oraz stan 'done'."""
        conn = self.get_conn()
        try:
            cur = conn.cursor()

            # 1. Wczytanie taksonomii wymiarów
            cur.execute("SELECT id, segment, makro_dzial, tag FROM dims ORDER BY id ASC")
            rows_dims = cur.fetchall()
            self.dims_list = []
            for r in rows_dims:
                dim_id = int(r["id"])
                seg = str(r["segment"])
                makro = str(r["makro_dzial"]).strip()
                tag = str(r["tag"]).strip()

                d_obj = {"id": dim_id, "segment": seg, "makro_dzial": makro, "tag": tag}
                self.dims_list.append(d_obj)
                self.tag_to_id[tag] = dim_id

                norm_tag = self._normalize_text(tag)
                self.norm_tag_to_id[norm_tag] = dim_id

                if seg in self.segments_dict:
                    self.segments_dict[seg].append(dim_id)
                else:
                    self.segments_dict[seg] = [dim_id]

                norm_makro = self._normalize_text(makro)
                if norm_makro not in self.makro_to_ids:
                    self.makro_to_ids[norm_makro] = []
                self.makro_to_ids[norm_makro].append(dim_id)

            # 2. Wczytanie macierzy wektorów z tabeli 'main'
            cur.execute("SELECT id, vector FROM main ORDER BY id ASC")
            rows_main = cur.fetchall()

            id_list = []
            vec_list = []
            for r in rows_main:
                id_list.append(int(r[0]))
                v = np.frombuffer(r[1], dtype=np.float32)
                vec_list.append(v)

            if vec_list:
                self.ids = np.array(id_list, dtype=np.int32)
                self.vectors = np.array(vec_list, dtype=np.float32)
                self.norms = np.linalg.norm(self.vectors, axis=1).astype(np.float32)
                self.id_to_idx = {int(tid): i for i, tid in enumerate(self.ids)}

                # Obliczenie rzutu PCA 160D -> 3D (baza wektorów wiedzy)
                self.pca_mean = np.mean(self.vectors, axis=0)
                centered = self.vectors - self.pca_mean
                _, S, vt = np.linalg.svd(centered, full_matrices=False)
                self.pca_basis = vt[:3, :].T.astype(np.float32)
                total_var = float((S ** 2).sum())
                self.pca_var_ratio = float((S[:3] ** 2).sum() / total_var) if total_var > 0 else 0.0

            # 3. Wczytanie zrobionych zadań
            cur.execute("SELECT id FROM done")
            self.done_set = {int(row[0]) for row in cur.fetchall()}
        finally:
            conn.close()

    @staticmethod
    def _normalize_text(s: str) -> str:
        s = s.lower().strip()
        pl_map = str.maketrans("ąćęłńóśźż", "acelnoszz")
        s = s.translate(pl_map)
        s = re.sub(r"[^a-z0-9]", "", s)
        return s

    def get_cooldown_failed_ids(self) -> Set[int]:
        """Zwraca zbiór zadań nieudanych, które są w okresie kwarantanny (3 dni)."""
        conn = self.get_conn()
        try:
            cur = conn.cursor()
            cutoff = time.time() - (FAIL_COOLDOWN_DAYS * 86400.0)
            cur.execute("SELECT id FROM cooldown_fails WHERE failed_at > ?", (cutoff,))
            return {int(row[0]) for row in cur.fetchall()}
        finally:
            conn.close()

    def resolve_dim(self, name_or_id: Union[str, int]) -> int:
        """Konwertuje nazwę tagu, słowo kluczowe lub indeks na liczbę 0..159."""
        if isinstance(name_or_id, int):
            if 0 <= name_or_id < NUM_DIMS:
                return name_or_id
            raise MaturaError(f"Indeks wymiaru poza zakresem 0..{NUM_DIMS-1}: {name_or_id}")

        if str(name_or_id).isdigit():
            idx = int(name_or_id)
            if 0 <= idx < NUM_DIMS:
                return idx
            raise MaturaError(f"Indeks wymiaru poza zakresem 0..{NUM_DIMS-1}: {name_or_id}")

        s = str(name_or_id).strip()
        if s in self.tag_to_id:
            return self.tag_to_id[s]

        norm_s = self._normalize_text(s)
        if norm_s in self.norm_tag_to_id:
            return self.norm_tag_to_id[norm_s]

        if norm_s in self.makro_to_ids:
            return self.makro_to_ids[norm_s][0]

        best_match = None
        best_score = 0
        for norm_tag, dim_id in self.norm_tag_to_id.items():
            if norm_s in norm_tag:
                score = len(norm_s) / max(len(norm_tag), 1)
                if score > best_score:
                    best_score = score
                    best_match = dim_id

        if best_match is not None:
            return best_match

        for norm_makro, dims in self.makro_to_ids.items():
            if norm_s in norm_makro or norm_makro in norm_s:
                return dims[0]

        raise MaturaError(f"Nie rozpoznano wymiaru '{name_or_id}'. Wywołaj Wymiary(), aby przejrzeć listę.")

_engine = _StorageEngine.get()


# ==============================================================================
# GŁÓWNE API DLA CLAUDE'A
# ==============================================================================

def Znajdz(
    vector: Union[List[float], Dict[Union[str, int], float], np.ndarray],
    length: Optional[float] = None,
    samples: int = 1,
    **filtry
) -> Union[List[int], List[Tuple[int, float]]]:
    """Wyszukuje ID zadań niezrobionych, najbliższych profilowi tematycznemu i docelowej trudności length (norma 1.5 - 6.0+).

    Przykład:
        Znajdz({'ciąg geometryczny': 3.0}, length=3.5, samples=2)  # -> [3087, 3882]
    """
    if _engine.vectors.shape[0] == 0:
        raise MaturaError("Baza zadań jest pusta.")

    # 1. Parsowanie wektora zapytania
    if isinstance(vector, dict):
        q_vec = np.array(Wektor(vector), dtype=np.float32)
    elif isinstance(vector, (list, tuple, np.ndarray)):
        q_vec = np.array(vector, dtype=np.float32)
        if len(q_vec) != NUM_DIMS:
            raise MaturaError(f"Wektor musi mieć dokładnie {NUM_DIMS} wymiarów (podano {len(q_vec)}).")
    else:
        raise MaturaError("Parametr vector musi być słownikiem rzadkim lub listą 160 floatów.")

    q_norm = float(np.linalg.norm(q_vec))

    # 2. Docelowa norma trudności (length)
    if length is not None:
        target_length = float(length)
    elif q_norm > 0:
        target_length = q_norm
    else:
        target_length = 3.0  # domyślny poziom trudności maturalnej

    # 3. Wagi i parametry
    w_kat = float(filtry.get("w_kat", DEFAULT_W_KAT))
    w_dl = float(filtry.get("w_dl", DEFAULT_W_DL))
    dim_weights = filtry.get("dim_weights", None)

    # 4. Kąt kosinusowy
    if dim_weights:
        w_diag = np.ones(NUM_DIMS, dtype=np.float32)
        for d_key, d_w in dim_weights.items():
            dim_idx = _engine.resolve_dim(d_key)
            w_diag[dim_idx] = float(d_w)
        scaled_q = q_vec * w_diag
        scaled_mat = _engine.vectors * w_diag
    else:
        scaled_q = q_vec
        scaled_mat = _engine.vectors

    scaled_q_norm = float(np.linalg.norm(scaled_q))
    if scaled_q_norm > 1e-7:
        scaled_mat_norms = np.linalg.norm(scaled_mat, axis=1)
        scaled_mat_norms = np.maximum(scaled_mat_norms, 1e-7)
        cos_sim = np.dot(scaled_mat, scaled_q) / (scaled_mat_norms * scaled_q_norm)
        cos_sim = np.clip(cos_sim, -1.0, 1.0)
        angles = np.arccos(cos_sim)
    else:
        angles = np.zeros(_engine.vectors.shape[0], dtype=np.float32)

    # 5. Różnica trudności (norma euklidesowa)
    norm_diffs = np.abs(_engine.norms - target_length)
    distances = (w_kat * angles) + (w_dl * norm_diffs)

    # 6. Filtrowanie (rekordy bez wektora to wstępy stron i śmieci OCR, nie zadania)
    mask = _engine.norms > 0.5

    if not bool(filtry.get("include_done", False)) and _engine.done_set:
        done_indices = [_engine.id_to_idx[tid] for tid in _engine.done_set if tid in _engine.id_to_idx]
        mask[done_indices] = False

    if not bool(filtry.get("include_fails", False)):
        cooldown_ids = _engine.get_cooldown_failed_ids()
        if cooldown_ids:
            cd_indices = [_engine.id_to_idx[tid] for tid in cooldown_ids if tid in _engine.id_to_idx]
            mask[cd_indices] = False

    exclude_ids = set(filtry.get("exclude", []))
    if exclude_ids:
        ex_indices = [_engine.id_to_idx[tid] for tid in exclude_ids if tid in _engine.id_to_idx]
        mask[ex_indices] = False

    if filtry.get("only_dims"):
        for d in filtry["only_dims"]:
            d_idx = _engine.resolve_dim(d)
            mask &= (_engine.vectors[:, d_idx] > 0.0)

    if filtry.get("forbid_dims"):
        for d in filtry["forbid_dims"]:
            d_idx = _engine.resolve_dim(d)
            mask &= (_engine.vectors[:, d_idx] == 0.0)

    if filtry.get("level"):
        lvl = str(filtry["level"]).lower().strip()
        conn = _engine.get_conn()
        try:
            cur = conn.cursor()
            cur.execute('SELECT id FROM "desc" WHERE level = ?', (lvl,))
            allowed_ids = {row[0] for row in cur.fetchall()}
            mask &= np.array([tid in allowed_ids for tid in _engine.ids], dtype=bool)
        finally:
            conn.close()

    candidate_indices = np.where(mask)[0]
    if len(candidate_indices) == 0:
        return []

    cand_dists = distances[candidate_indices]

    # 7. Losowość / Diversity (MMR)
    randomness = float(filtry.get("randomness", DEFAULT_RANDOMNESS))
    diversity = float(filtry.get("diversity", 0.0))
    return_scores = bool(filtry.get("return_scores", False))

    if diversity > 0.0 and samples > 1:
        chosen_indices = []
        pool = list(candidate_indices[np.argsort(cand_dists)[:max(samples * 6, 25)]])

        while len(chosen_indices) < samples and pool:
            if not chosen_indices:
                chosen_indices.append(pool.pop(0))
                continue

            chosen_mat = _engine.vectors[chosen_indices]
            chosen_norms = np.maximum(_engine.norms[chosen_indices], 1e-7)

            best_score = float("inf")
            best_idx = 0
            for p_pos, p_idx in enumerate(pool):
                p_vec = _engine.vectors[p_idx]
                p_norm = max(_engine.norms[p_idx], 1e-7)
                sims = np.dot(chosen_mat, p_vec) / (chosen_norms * p_norm)
                max_sim = float(np.max(sims))

                mmr = (1.0 - diversity) * distances[p_idx] + diversity * (max_sim * 2.0)
                if mmr < best_score:
                    best_score = mmr
                    best_idx = p_pos

            chosen_indices.append(pool.pop(best_idx))
    elif randomness > 0.0 and len(candidate_indices) > 1:
        top_k = min(len(candidate_indices), max(samples * 5, 25))
        top_order = np.argsort(cand_dists)[:top_k]
        top_indices = candidate_indices[top_order]
        top_dists = cand_dists[top_order]

        temp = max(randomness * (np.std(top_dists) if np.std(top_dists) > 1e-4 else 0.2), 0.05)
        weights = np.exp(-(top_dists - np.min(top_dists)) / temp)
        probs = weights / np.sum(weights)

        chosen_sub = np.random.choice(len(top_indices), size=min(samples, len(top_indices)), replace=False, p=probs)
        chosen_indices = top_indices[chosen_sub]
    else:
        chosen_indices = candidate_indices[np.argsort(cand_dists)[:samples]]

    if return_scores:
        return [(int(_engine.ids[i]), round(float(distances[i]), 4)) for i in chosen_indices]
    return [int(_engine.ids[i]) for i in chosen_indices]


def Pobierz(ids: Union[List[int], int], type: str = "text") -> str:
    """Zwraca treść matematyczną LaTeX lub link do zdjęcia zadania.

    Przykład:
        Pobierz([3087, 3882], type='both')  # -> '3087: Treść...\n3087 [FOTO]: ...'
    """
    if isinstance(ids, int):
        id_list = [ids]
    elif isinstance(ids, (list, tuple, set)):
        id_list = [int(x) for x in ids]
    else:
        raise MaturaError("Parametr ids musi być liczbą int lub listą intów.")

    if not id_list:
        return ""

    mode = type.lower().strip()
    if mode not in ("text", "photos", "both"):
        raise MaturaError(f"Nieznany typ '{type}'. Dostępne: 'text', 'photos', 'both'.")

    text_map = {}
    photo_map = {}

    conn = _engine.get_conn()
    try:
        cur = conn.cursor()
        chunk_size = 500
        for i in range(0, len(id_list), chunk_size):
            chunk = id_list[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)

            if mode in ("text", "both"):
                cur.execute(f'SELECT id, latex FROM "desc" WHERE id IN ({placeholders})', chunk)
                for row in cur.fetchall():
                    text_map[row[0]] = row[1]

            if mode in ("photos", "both"):
                cur.execute(f"SELECT id, url FROM photos WHERE id IN ({placeholders})", chunk)
                for row in cur.fetchall():
                    photo_map[row[0]] = row[1]
    finally:
        conn.close()

    lines = []
    for tid in id_list:
        if tid not in _engine.id_to_idx:
            lines.append(f"{tid}: [BRAK ZADANIA]")
            continue

        if mode == "text":
            txt = text_map.get(tid, "[BRAK TREŚCI]")
            lines.append(f"{tid}: {txt}")
        elif mode == "photos":
            photo = photo_map.get(tid, "[BRAK ZDJĘCIA]")
            lines.append(f"{tid}: {photo}")
        elif mode == "both":
            txt = text_map.get(tid, "[BRAK TREŚCI]")
            photo = photo_map.get(tid, "[BRAK ZDJĘCIA]")
            lines.append(f"{tid}: {txt}\n{tid} [FOTO]: {photo}")

    return "\n".join(lines)


_PKT_NAGLOWEK = re.compile(r"(?i)zadanie\s*\d+\s*\.?\s*\(([^)]{1,20})\)")
_PKT_ZAKRES = re.compile(r"\d+\s*[–—−-]\s*(\d+)")
_PKT_LICZBA = re.compile(r"(?i)(\d+)\s*(?:pkt|p\b|p\.|punkt)")


def _punkty_z_tekstu(t: str) -> Optional[int]:
    """Maks. punkty z nagłówka: 'ZADANIE 4 (5 PKT)', 'Zadanie 4. (0–5)', '(4 p.)', '(0-3 pkt)', '( 2p )'."""
    t = t or ""
    m = _PKT_NAGLOWEK.search(t[:200])
    kandydaci = [m.group(1)] if m else []
    kandydaci += re.findall(r"\(([^)]{1,20})\)", t[:120])
    for k in kandydaci:
        z = _PKT_ZAKRES.search(k)
        if z and ("pkt" in k.lower() or re.fullmatch(r"\s*0\s*[–—−-]\s*\d+\s*", k)):
            return int(z.group(1))
        z = _PKT_LICZBA.search(k)
        if z:
            return int(z.group(1))
    # Podpunkty matur 2023+ mają punkty dalej w treści: „12.1. (0–2)”.
    z = re.search(r"\((\d+)\s*PKT", t, re.I) or re.search(r"\(0\s*[–-]\s*(\d+)\)", t)
    return int(z.group(1)) if z else None


def Punkty(id: int) -> Optional[int]:
    """Maksymalna liczba punktów zadania odczytana z nagłówka treści (None, gdy jej tam nie ma).

    Przykład:
        Punkty(517)  # 'ZADANIE 1 (3 PKT)' -> 3
    """
    conn = _engine.get_conn()
    try:
        r = conn.execute('SELECT latex FROM "desc" WHERE id = ?', (int(id),)).fetchone()
    finally:
        conn.close()
    return _punkty_z_tekstu(r[0] if r else "")


def Zamkniete(id: int) -> bool:
    """True dla zadania zamkniętego (odpowiedzi A–D albo 1 pkt). Na maturze od 2023 ich nie ma."""
    conn = _engine.get_conn()
    try:
        r = conn.execute('SELECT latex FROM "desc" WHERE id = ?', (int(id),)).fetchone()
    finally:
        conn.close()
    t = (r[0] if r else "") or ""
    if _punkty_z_tekstu(t) == 1:
        return True
    return bool(re.search(r"\bA[.)]\s.{0,200}\bB[.)]\s.{0,200}\bC[.)]\s.{0,200}\bD[.)]\s", t, re.S))


def _extract_task_score(result_str: str, note_str: str) -> float:
    """Pomocnik do odczytywania punktacji (1..10) z tabeli done/history."""
    if note_str:
        m = re.search(r"\[pkt:\s*([0-9.]+)", note_str, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        m2 = re.search(r"\[score:\s*([0-9.]+)", note_str, re.IGNORECASE)
        if m2:
            try:
                return float(m2.group(1))
            except ValueError:
                pass
    r = result_str.lower().strip()
    if r.replace('.', '', 1).isdigit():
        try:
            return float(r)
        except ValueError:
            pass
    if r == "ok":
        return 10.0
    elif r == "partial":
        return 5.0
    elif r == "fail":
        return 1.0
    return 8.0


def Odnotuj(id: int, result: Union[str, int, float] = "ok", note: str = "", score: Optional[float] = None) -> None:
    """Zapisuje wynik rozwiązania zadania przez ucznia (lub punktację 1-10) do historii oraz aktualizuje status w bazie.

    result:
      - 'ok': rozwiązane samodzielnie i poprawnie (10 pkt, trafia do done i znika z Znajdz)
      - 'partial': rozwiązane z pomocą / z błędem (5 pkt, trafia do done, w historii widnieje jako słabe)
      - 'fail': nierozwiązane (1 pkt, wykluczone z Znajdz na 3 dni kwarantanny, po czym wraca jako powtórka)
      - liczba 0-10 lub napis liczbowy (0->fail, 1..4->partial, 5..10->ok z zachowaniem dokładnej oceny)

    note:
      - opcjonalna krótka notatka dla Claude'a, np. "pomylił dziedzinę logarytmu"

    score:
      - opcjonalna bezpośrednia punktacja 0..10.
    """
    tid = int(id)
    if tid not in _engine.id_to_idx:
        raise MaturaError(f"Nie znaleziono zadania o ID {tid}.")

    exact_score: Optional[float] = None
    if score is not None:
        exact_score = float(score)
    elif isinstance(result, (int, float)):
        exact_score = float(result)
    elif str(result).replace('.', '', 1).isdigit():
        exact_score = float(result)

    note_str = str(note).strip()

    if exact_score is not None:
        exact_score = max(0.0, min(10.0, exact_score))
        if exact_score <= 0:
            res_str = "fail"
        elif exact_score <= 4:
            res_str = "partial"
        else:
            res_str = "ok"
        score_disp = int(exact_score) if exact_score.is_integer() else round(exact_score, 1)
        if f"[pkt: {score_disp}" not in note_str and f"[score: {score_disp}" not in note_str:
            note_str = f"[pkt: {score_disp}/10] {note_str}".strip() if note_str else f"[pkt: {score_disp}/10]"
    else:
        res_str = str(result).lower().strip()
        if res_str not in ("ok", "partial", "fail"):
            raise MaturaError(f"Nieprawidłowy wynik '{result}'. Dozwolone: 'ok', 'partial', 'fail' (lub ocena 0..10).")
    ts = time.time()

    conn = _engine.get_conn()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT attempt FROM history WHERE id = ? ORDER BY entry_id DESC LIMIT 1", (tid,))
            row = cur.fetchone()
            attempt = (row[0] + 1) if row else 1

            if res_str in ("ok", "partial"):
                cur.execute("""
                    INSERT INTO done (id, result, note, attempt, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        result = excluded.result,
                        note = excluded.note,
                        attempt = excluded.attempt,
                        timestamp = excluded.timestamp
                """, (tid, res_str, note_str, attempt, ts))
                cur.execute("DELETE FROM cooldown_fails WHERE id = ?", (tid,))
                _engine.done_set.add(tid)
            else:  # fail
                cur.execute("DELETE FROM done WHERE id = ?", (tid,))
                cur.execute("""
                    INSERT INTO cooldown_fails (id, failed_at)
                    VALUES (?, ?)
                    ON CONFLICT(id) DO UPDATE SET failed_at = excluded.failed_at
                """, (tid, ts))
                _engine.done_set.discard(tid)

            cur.execute("""
                INSERT INTO history (id, result, note, attempt, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (tid, res_str, note_str, attempt, ts))
    finally:
        conn.close()


def Ostatnie(n: int = 20, id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Zwraca pamięć ucznia: ostatnie próby rozwiązywania zadań z wynikami ('ok', 'partial', 'fail') i notatkami.

    Przykład:
        Ostatnie(5)  # -> [{'id': 3087, 'wynik': 'partial', 'notatka': 'błąd dziedziny', ...}, ...]
    """
    conn = _engine.get_conn()
    try:
        cur = conn.cursor()
        if id is not None:
            cur.execute("""
                SELECT h.id, d.name, h.result, h.note, h.attempt, h.timestamp, d.difficulty
                FROM history h LEFT JOIN "desc" d ON h.id = d.id
                WHERE h.id = ?
                ORDER BY h.entry_id DESC LIMIT ?
            """, (int(id), int(n)))
        else:
            cur.execute("""
                SELECT h.id, d.name, h.result, h.note, h.attempt, h.timestamp, d.difficulty
                FROM history h LEFT JOIN "desc" d ON h.id = d.id
                ORDER BY h.entry_id DESC LIMIT ?
            """, (int(n),))

        rows = cur.fetchall()
    finally:
        conn.close()

    history_items = []
    for r in rows:
        tid = r[0]
        topics = []
        if tid in _engine.id_to_idx:
            idx = _engine.id_to_idx[tid]
            v = _engine.vectors[idx]
            top_dims = np.argsort(v)[::-1][:2]
            topics = [_engine.dims_list[d]["tag"] for d in top_dims if v[d] > 0 and d < len(_engine.dims_list)]

        dt_str = _data(r[5]).strftime("%Y-%m-%d %H:%M")
        history_items.append({
            "id": tid,
            "nazwa": r[1] or f"ZAD_{tid}",
            "wynik": r[2],
            "notatka": r[3] or "",
            "proba": r[4],
            "data": dt_str,
            "trudnosc": round(float(r[6]), 2) if r[6] else 0.0,
            "tematy": topics,
        })

    return history_items


def Wymiary(segment: Optional[str] = None) -> Dict[str, int]:
    """Zwraca listę 160 wymiarów/tagów (nazwa -> indeks), opcjonalnie filtrując po segmencie ('A', 'B', 'C').

    Przykład:
        Wymiary('A')  # -> {'Działania w zbiorze liczb rzeczywistych...': 0, ...}
    """
    if segment:
        seg = segment.upper().strip()
        if seg not in ("A", "B", "C"):
            raise MaturaError(f"Nieznany segment '{segment}'. Dostępne segmenty: 'A', 'B', 'C'.")
        allowed_ids = set(_engine.segments_dict.get(seg, []))
        return {w["tag"]: int(w["id"]) for w in _engine.dims_list if int(w["id"]) in allowed_ids}
    return {w["tag"]: int(w["id"]) for w in _engine.dims_list}


def ZadaniaPodobne(id: int, samples: int = 1, mode: str = "twin") -> List[int]:
    """Skrót do Znajdz: wyszukuje zadania powiązane ('twin', 'harder', 'easier', 'same_tools').

    Przykład:
        ZadaniaPodobne(3087, samples=2, mode='harder')  # -> [2535, 1008]
    """
    tid = int(id)
    if tid not in _engine.id_to_idx:
        raise MaturaError(f"Nie znaleziono zadania o ID {tid}.")

    idx = _engine.id_to_idx[tid]
    vec = _engine.vectors[idx]
    norm = float(_engine.norms[idx])
    m = mode.lower().strip()

    if m == "twin":
        return Znajdz(vec, length=norm, samples=samples, exclude=[tid])
    elif m == "harder":
        return Znajdz(vec, length=norm * 1.3, samples=samples, exclude=[tid])
    elif m == "easier":
        return Znajdz(vec, length=max(1.5, norm * 0.75), samples=samples, exclude=[tid])
    elif m == "same_tools":
        b_dims = _engine.segments_dict.get("B", list(range(120, 154)))
        active_b = [d for d in b_dims if vec[d] > 0.0]
        if active_b:
            return Znajdz(vec, length=norm, samples=samples, exclude=[tid], only_dims=active_b)
        return Znajdz(vec, length=norm, samples=samples, exclude=[tid])
    else:
        raise MaturaError(f"Nieznany tryb '{mode}'. Dostępne: 'twin', 'harder', 'easier', 'same_tools'.")


# ==============================================================================
# FUNKCJE POMOCNICZE
# ==============================================================================

def Wektor(spec: Dict[Union[str, int], float]) -> List[float]:
    """Buduje pełny wektor R^160 z rzadkiego słownika {nazwa_lub_indeks: wartość}.

    Jeśli klucz to nazwa makro-działu (np. 'stereometria', 'funkcjakwadratowa'),
    waga jest przypisywana do wszystkich wchodzących w jego skład wymiarów CKE.

    Przykład:
        Wektor({'ciąg arytmetyczny': 3.0, 120: 2.0})
        Wektor({'stereometria': 3.0})
    """
    if not isinstance(spec, dict):
        raise MaturaError("Argument spec musi być słownikiem, np. {'funkcja liniowa': 2.5}.")

    vec = np.zeros(NUM_DIMS, dtype=np.float32)
    for key, val in spec.items():
        if isinstance(key, str):
            norm_k = _engine._normalize_text(key)
            if norm_k in _engine.makro_to_ids:
                dims = _engine.makro_to_ids[norm_k]
                for d in dims:
                    vec[d] = max(vec[d], float(val))
                continue
        dim_idx = _engine.resolve_dim(key)
        vec[dim_idx] = float(val)
    return [round(float(x), 4) for x in vec]


def Info(id: int) -> Dict[str, Any]:
    """Zwraca metadane zadania: nazwę, aktywne tagi z wartościami, normę trudności i ostatni wynik.

    Przykład:
        Info(3087)  # -> {'id': 3087, 'nazwa': '...', 'norma': 3.55, 'aktywne_tagi': {...}, ...}
    """
    tid = int(id)
    if tid not in _engine.id_to_idx:
        raise MaturaError(f"Nie znaleziono zadania o ID {tid}.")

    idx = _engine.id_to_idx[tid]
    vec = _engine.vectors[idx]
    norm = float(_engine.norms[idx])

    active_tags = {}
    for d_idx in np.where(vec > 0.0)[0]:
        tag_name = _engine.dims_list[d_idx]["tag"] if d_idx < len(_engine.dims_list) else str(d_idx)
        active_tags[tag_name] = round(float(vec[d_idx]), 2)

    conn = _engine.get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT name, status, solution FROM "desc" WHERE id = ?', (tid,))
        row = cur.fetchone()
        name = row[0] if row else f"ZAD_{tid}"
        solution = row[2] if row else ""

        cur.execute("SELECT result, note FROM done WHERE id = ?", (tid,))
        d_row = cur.fetchone()
        try:
            auto = cur.execute("SELECT pewnosc FROM wektory_auto WHERE id = ?", (tid,)).fetchone()
        except sqlite3.OperationalError:
            auto = None
        latex = cur.execute('SELECT latex FROM "desc" WHERE id = ?', (tid,)).fetchone()
        last_result = d_row[0] if d_row else ("zrobione" if tid in _engine.done_set else "nowe")
        last_note = d_row[1] if d_row else ""
    finally:
        conn.close()

    if not solution or solution.startswith("Zwektoryzowano"):
        solution = ""  # placeholder, nie szkic rozwiązania
    return {
        "id": tid,
        "nazwa": name,
        "norma": round(norm, 2),
        "punkty": _punkty_z_tekstu(latex[0] if latex else ""),
        "status": last_result,
        "notatka": last_note,
        "aktywne_tagi": active_tags,
        "wektor": f"uzupełniony z podobnych zadań (pewność {auto[0]:.2f})" if auto else "oryginalny",
        "opis_krotki": solution[:150] + "..." if len(solution) > 150 else solution,
    }


def Statystyki() -> Dict[str, Any]:
    """Zwraca ogólne statystyki bazy i wykonanych zadań.

    Przykład:
        Statystyki()  # -> {'wszystkie_zadania': 4608, 'zrobione_ok': 15, 'zrobione_partial': 4, ...}
    """
    total = len(_engine.ids)

    conn = _engine.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT result, COUNT(*) FROM done GROUP BY result")
        counts = dict(cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM history")
        total_attempts = cur.fetchone()[0]
    finally:
        conn.close()

    ok_count = counts.get("ok", 0)
    partial_count = counts.get("partial", 0)
    done_count = len(_engine.done_set)

    return {
        "wszystkie_zadania": total,
        "zrobione": done_count,
        "sukces_samodzielny": ok_count,
        "zrobione_z_pomoca": partial_count,
        "pozostale_nowe": total - done_count,
        "laczna_liczba_prob": total_attempts,
    }


def Slabe(limit: int = 5, min_attempts: int = 1, segmenty: str = "AB") -> List[Dict[str, Any]]:
    """Analizuje historię prób i zwraca tematy z największą liczbą błędów lub najniższą skutecznością.

    Parametry:
        limit: maksymalna liczba zwracanych tematów (domyślnie 5).
        min_attempts: minimalna liczba prób na dany wymiar (domyślnie 1).
                      Ustawienie min_attempts=2 zapobiega dominacji rankingu przez pojedynczą losową wpadkę.
        segmenty: które segmenty wymiarów oceniać (domyślnie 'AB': działy i narzędzia; 'C' to styl
                  zadania, np. „obciążenie rachunkowe”, i zwykle tylko zaśmieca ranking).

    Skuteczność liczy się z punktów zapisanych w notatce ([pkt: x/10], jak w plan.Zapisz), a gdy ich
    nie ma, z wyniku: ok = 10, partial = 5, fail = 1.

    Przykład:
        Slabe(3, min_attempts=1)  # -> [{'wymiar': 'Logarytmy...', 'porazki': 3, 'skutecznosc': '25%'}, ...]
    """
    conn = _engine.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, result, note FROM history ORDER BY entry_id DESC LIMIT 200")
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    allowed = {d for seg in segmenty.upper() for d in _engine.segments_dict.get(seg, [])}
    dim_stats: Dict[int, Dict[str, float]] = {}
    for tid, res, note in rows:
        if tid not in _engine.id_to_idx:
            continue
        idx = _engine.id_to_idx[tid]
        v = _engine.vectors[idx]
        score = _extract_task_score(str(res), note or "")
        active_dims = [d for d in np.where(v > 0.0)[0] if int(d) in allowed]
        for d in active_dims:
            if d not in dim_stats:
                dim_stats[d] = {"attempts": 0, "fails": 0, "partial": 0, "ok": 0, "score": 0.0}
            dim_stats[d]["attempts"] += 1
            dim_stats[d]["score"] += score
            if res == "fail":
                dim_stats[d]["fails"] += 1
            elif res == "partial":
                dim_stats[d]["partial"] += 1
            else:
                dim_stats[d]["ok"] += 1

    ranked = []
    for d, st in dim_stats.items():
        if st["attempts"] < max(1, int(min_attempts)):
            continue
        tag = _engine.dims_list[d]["tag"] if d < len(_engine.dims_list) else str(d)
        succ_rate = round(st["score"] / st["attempts"] * 10, 1)
        ranked.append({
            "wymiar": tag,
            "dzial": _engine.dims_list[d]["makro_dzial"] if d < len(_engine.dims_list) else "",
            "porazki": int(st["fails"]),
            "z_pomoca": int(st["partial"]),
            "proby": int(st["attempts"]),
            "skutecznosc": f"{succ_rate}%",
            "_raw_rate": succ_rate
        })

    # Sortujemy: priorytet mają tematy z faktycznymi porażkami (fails desc),
    # a przy remisie najniższa skuteczność (succ_rate asc)
    ranked.sort(key=lambda x: (-x["porazki"], x["_raw_rate"]))
    for item in ranked:
        item.pop("_raw_rate", None)
    return ranked[:limit]


# ==============================================================================
# SFERYCZNE HARMONIKI I KRAJOBRAZ KOMPETENCJI (3D <-> 160D)
# ==============================================================================

def cartesian_to_spherical(points: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Konwertuje punkty 3D na współrzędne sferyczne względem środka sfery wiedzy [0, 0, 0]."""
    center = np.zeros(3, dtype=np.float32)
    diff = points - center
    r = np.linalg.norm(diff, axis=1)
    phi = np.arccos(np.clip(diff[:, 2] / np.where(r == 0, 1e-10, r), -1.0, 1.0))
    theta = np.arctan2(diff[:, 1], diff[:, 0]) % (2 * np.pi)
    return center, r, theta, phi


def real_sph_harm(m: int, l: int, theta: Union[float, np.ndarray], phi: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
    """Rzeczywiste harmoniki sferyczne Y_lm(theta, phi).
    theta: azymut [0, 2*pi]
    phi: kąt biegunowy [0, pi]
    """
    val = _sph_harm_calc(abs(m), l, theta, phi)
    if m < 0:
        return float(np.sqrt(2)) * ((-1)**m) * np.imag(val)
    elif m == 0:
        return np.real(val)
    else:
        return float(np.sqrt(2)) * ((-1)**m) * np.real(val)


def project_160_to_3d(vector: Union[List[float], Dict[Union[str, int], float], np.ndarray]) -> np.ndarray:
    """Rzutuje wektor 160D (lub rzadki słownik tagów) na jednostkowy wektor kierunkowy 3D [x, y, z].

    Przykład:
        u_3d = project_160_to_3d({'stereometria': 3.0})
    """
    if isinstance(vector, dict):
        v = np.array(Wektor(vector), dtype=np.float32)
    else:
        v = np.array(vector, dtype=np.float32)
        if len(v) != NUM_DIMS:
            raise MaturaError(f"Wektor musi mieć dokładnie {NUM_DIMS} wymiarów (podano {len(v)}).")

    diff = v - _engine.pca_mean
    p3d = diff @ _engine.pca_basis
    norm = float(np.linalg.norm(p3d))
    if norm > 1e-6:
        return (p3d / norm).astype(np.float32)
    return np.array([0.0, 0.0, 1.0], dtype=np.float32)


def project_3d_to_160(vector_3d: Union[List[float], np.ndarray], top_k: Optional[int] = 5) -> Dict[str, Any]:
    """Transformuje wektor kierunkowy 3D na pełny wektor 160D oraz wyznacza dominujące tagi wraz z ich stopniem udziału.

    Zwraca:
        - 'wektor_160': lista 160 floatów w przestrzeni CKE
        - 'tagi': słownik {nazwa_tagu: procentowy_stopień_udziału}
        - 'napis': czytelny opis tekstowy
    """
    u = np.array(vector_3d, dtype=np.float32)
    norm = float(np.linalg.norm(u))
    if norm > 1e-6:
        u = u / norm

    v160 = u @ _engine.pca_basis.T

    pos_idx = np.where(v160 > 0.0)[0]
    if len(pos_idx) > 0:
        sorted_pos = pos_idx[np.argsort(v160[pos_idx])[::-1]]
        tot_weight = float(np.sum(v160[pos_idx]))
        tags_dict = {}
        k = top_k if top_k is not None else len(sorted_pos)
        for idx in sorted_pos[:k]:
            tag_name = _engine.dims_list[idx]["tag"] if idx < len(_engine.dims_list) else str(idx)
            pct = round((float(v160[idx]) / tot_weight) * 100.0, 1)
            tags_dict[tag_name] = pct
    else:
        tags_dict = {}

    parts = [f"{tag} ({deg}%)" for tag, deg in tags_dict.items()]
    napis = ", ".join(parts) if parts else "Brak wyraźnych tagów dominujących"

    return {
        "wektor_160": [round(float(x), 4) for x in v160],
        "tagi": tags_dict,
        "napis": napis,
    }


def fit_spherical_harmonics(
    points: np.ndarray,
    L: Optional[int] = None,
    alpha: float = 0.08
) -> Tuple[List[Tuple[int, int]], np.ndarray, Dict[str, Any]]:
    """Dopasowuje harmoniki sferyczne z regularyzacją Laplace'a-Beltramiego
    oraz analityczną estymacją błędu generalizacji LOOCV (Leave-One-Out CV).
    """
    N = len(points)
    if N == 0:
        raise MaturaError("Brak punktów do dopasowania harmonik sferycznych.")

    center, r, theta, phi = cartesian_to_spherical(points)

    # Adaptacyjny dobór stopnia L do liczby próbek
    if L is None:
        if N < 5:
            L = 1   # 4 parametry
        elif N < 14:
            L = 1   # 4 parametry (dipol: poziom bazowy + 1 główny biegun)
        elif N < 28:
            L = 2   # 9 parametrów (kwadrupol: 2-3 wyraźne ekstrema)
        else:
            L = 3   # 16 parametrów (pełny krajobraz)

    terms = []
    basis = []
    for l_deg in range(L + 1):
        for m in range(-l_deg, l_deg + 1):
            terms.append((l_deg, m))
            basis.append(real_sph_harm(m, l_deg, theta, phi))
    A = np.column_stack(basis)

    K = len(terms)

    # Kara krzywizny Laplace'a-Beltramiego: l*(l+1) wygasza niekontrolowane oscylacje
    laplace_factors = np.array([1.0 + 1.5 * l_deg * (l_deg + 1) for l_deg, _ in terms], dtype=np.float32)
    reg_diag = alpha * laplace_factors
    reg_diag[0] = 0.0  # wyraz wolny l=0 (średni poziom ucznia) bez kary
    reg_mat = np.diag(reg_diag)

    ATA_reg = A.T @ A + reg_mat
    inv_ATA = np.linalg.pinv(ATA_reg)
    coeffs = inv_ATA @ (A.T @ r)

    # Predykcja na zbiorze treningowym
    r_pred = A @ coeffs
    residuals = r - r_pred
    rmse_train = float(np.sqrt(np.mean(residuals**2)))

    # Analityczna estymacja błędu uogólnienia LOOCV (Hat matrix)
    H = A @ inv_ATA @ A.T
    h_diag = np.clip(np.diag(H), 0.0, 0.90)
    cv_residuals = residuals / np.maximum(1.0 - h_diag, 0.10)
    rmse_cv = float(np.sqrt(np.mean(cv_residuals**2)))

    ss_tot = float(np.sum((r - np.mean(r)) ** 2))
    ss_res = float(np.sum(residuals**2))
    r2 = float(1.0 - (ss_res / ss_tot)) if ss_tot > 1e-6 else 1.0
    r2 = max(-1.0, min(1.0, r2))

    # Szacowany margines błędu w skali 1-10
    margines_bledu = round(rmse_cv, 2)

    # Analiza pokrycia sfery (8 oktantów)
    covered_octants = set()
    for p in points:
        oct_key = (1 if p[0] >= 0 else -1, 1 if p[1] >= 0 else -1, 1 if p[2] >= 0 else -1)
        covered_octants.add(oct_key)
    oct_count = len(covered_octants)
    coverage_pct = round((oct_count / 8.0) * 100.0, 1)

    # Analiza potrzebnej liczby zadań
    MIN_ZARYS = 5
    MIN_STABILNOSC = 15
    MIN_PRECYZJA = 30

    brakuje_do_stabilnosci = max(0, MIN_STABILNOSC - N)
    brakuje_do_precyzji = max(0, MIN_PRECYZJA - N)

    if N < MIN_ZARYS:
        wiarygodnosc = "Krytycznie niska (szacunek bardzo wstępny)"
        rekomendacja = f"Model w fazie startowej (N={N}/{MIN_ZARYS}). Zrób jeszcze min. {MIN_ZARYS - N} zadań na pierwszy zarys, a {MIN_STABILNOSC - N} do stabilnej mapy."
    elif N < MIN_STABILNOSC:
        wiarygodnosc = "Wstępna (ogólny trend mocnych/słabych stron)"
        rekomendacja = f"Faza wczesnej diagnozy (N={N}/{MIN_STABILNOSC}). Wykryto główne bieguny, ale do stabilnej mapy brakuje jeszcze {brakuje_do_stabilnosci} zadań."
    elif N < MIN_PRECYZJA:
        wiarygodnosc = "Dobra (stabilna mapa z wyraźnymi ekstremami)"
        rekomendacja = f"Model stabilny (N={N}/{MIN_PRECYZJA}). Główne minima i maksima są wiarygodne. Do pełnej precyzji brakuje {brakuje_do_precyzji} zadań."
    else:
        wiarygodnosc = "Wysoka (precyzyjna, ugruntowana mapa wiedzy)"
        rekomendacja = f"Wysoka precyzja (N={N}). Baza próbek jest w pełni reprezentatywna."

    stats = {
        "liczba_probek": N,
        "stopien_L": L,
        "liczba_funkcji_bazowych": K,
        "rmse_trening": round(rmse_train, 2),
        "rmse_cv_niepewnosc": round(rmse_cv, 2),
        "r2": round(r2, 2),
        "margines_bledu": margines_bledu,
        "pokrycie_sfery": f"{coverage_pct}%",
        "pokryte_oktanty": f"{oct_count}/8",
        "wiarygodnosc": wiarygodnosc,
        "wymogi_probek": {
            "aktualna_liczba": N,
            "minimum_do_zarysu": MIN_ZARYS,
            "minimum_do_stabilnosci": MIN_STABILNOSC,
            "optimum_do_precyzji": MIN_PRECYZJA,
            "brakuje_do_stabilnej_mapy": brakuje_do_stabilnosci,
        },
        "rekomendacja": rekomendacja,
        "opis": (
            f"Szacowana niepewność predykcji LOOCV wynosi +/-{margines_bledu} pkt na skali 1-10 "
            f"(RMSE trening: {rmse_train:.2f}, R^2: {r2:.2f}). Pokrycie przestrzeni: {coverage_pct}%. {rekomendacja}"
        ),
    }

    return terms, coeffs, stats


def find_critical_points(
    terms: List[Tuple[int, int]],
    coeffs: np.ndarray,
    top_tags: int = 4,
    n_fib: int = 600,
    min_sep_deg: float = 45.0
) -> List[Dict[str, Any]]:
    """Wyznacza ekstrema lokalne (minima / maksima) za pomocą quasi-jednorodnej siatki Fibonacciego
    oraz algorytmu Non-Maximum Suppression (NMS), rzutuje je na R^160 i podaje wagę tagów oraz najbliższe zadanie.
    """
    # 1. Równomierna siatka na sferze (Fibonacci Sphere - brak osobliwości biegunowych)
    i_idx = np.arange(0, n_fib, dtype=float) + 0.5
    phi_fib = np.arccos(1.0 - 2.0 * i_idx / n_fib)
    theta_fib = (np.pi * (1.0 + 5.0**0.5) * i_idx) % (2.0 * np.pi)

    x_fib = np.sin(phi_fib) * np.cos(theta_fib)
    y_fib = np.sin(phi_fib) * np.sin(theta_fib)
    z_fib = np.cos(phi_fib)
    fib_dirs = np.column_stack([x_fib, y_fib, z_fib])

    # 2. Obliczenie wysokości na sferze
    r_vals = np.zeros(n_fib, dtype=np.float32)
    for (l_deg, m), c in zip(terms, coeffs):
        r_vals += c * real_sph_harm(m, l_deg, theta_fib, phi_fib)

    r_vals = np.clip(r_vals, 0.5, 10.0)
    r_mean = float(np.mean(r_vals))

    # 3. Sąsiedztwo kątowe (~35 stopni)
    cos_thresh = float(np.cos(np.radians(35.0)))
    dot_prod = fib_dirs @ fib_dirs.T

    max_cands = []
    min_cands = []

    for idx in range(n_fib):
        neighbors = np.where(dot_prod[idx] >= cos_thresh)[0]
        val = float(r_vals[idx])
        if val == np.max(r_vals[neighbors]) and val > r_mean + 0.15:
            max_cands.append((val, fib_dirs[idx]))
        elif val == np.min(r_vals[neighbors]) and val < r_mean - 0.15:
            min_cands.append((val, fib_dirs[idx]))

    # 4. Non-Maximum Suppression z minimalnym odstępem min_sep_deg (np. 45 stopni)
    def _nms_extrema(candidates, is_max=True):
        candidates.sort(key=lambda x: x[0], reverse=is_max)
        kept = []
        min_cos = np.cos(np.radians(min_sep_deg))
        for val, u in candidates:
            if any(np.dot(u, kept_u) > min_cos for _, kept_u in kept):
                continue
            kept.append((val, u))
        return kept

    clean_max = _nms_extrema(max_cands, is_max=True)[:3]
    clean_min = _nms_extrema(min_cands, is_max=False)[:3]

    critical_points = []

    # Przetwarzanie minimów
    for val, u_3d in clean_min:
        desc160 = project_3d_to_160(u_3d, top_k=top_tags)
        height = round(float(val), 2)

        # Znajdź najbliższe zadanie w bazie do tego kierunku
        rekomendowane_id = None
        try:
            v_dir = np.array(desc160['wektor_160'], dtype=np.float32)
            v_target = np.maximum(_engine.pca_mean + 2.5 * v_dir, 0.0)
            found = Znajdz(v_target, length=3.2, samples=1, randomness=0.0)
            if found:
                rekomendowane_id = found[0]
        except Exception as e:
            logger.warning("Nie udało się dobrać zadania dla minimum: %s", e, exc_info=True)

        summary = f"MINIMUM (Wysokość: {height:.2f}/10) [Luka w wiedzy] — Tematy: {desc160['napis']}"
        if rekomendowane_id:
            summary += f" (Zadanie do powtórki: ID {rekomendowane_id})"

        critical_points.append({
            'typ': 'MINIMUM',
            'kategoria': 'Luka w wiedzy (wymaga powtórki)',
            'wysokosc': height,
            'odchylenie_od_sredniej': round(height - r_mean, 2),
            'kierunek_3d': [round(float(x), 3) for x in u_3d],
            'wektor_160d': desc160['wektor_160'],
            'tagi': desc160['tagi'],
            'opis': desc160['napis'],
            'rekomendowane_zadanie_id': rekomendowane_id,
            'podsumowanie': summary,
        })

    # Przetwarzanie maksimów
    for val, u_3d in clean_max:
        desc160 = project_3d_to_160(u_3d, top_k=top_tags)
        height = round(float(val), 2)

        summary = f"MAKSIMUM (Wysokość: {height:.2f}/10) [Mocna strona] — Tematy: {desc160['napis']}"

        critical_points.append({
            'typ': 'MAKSIMUM',
            'kategoria': 'Mocna strona (opanowany obszar)',
            'wysokosc': height,
            'odchylenie_od_sredniej': round(height - r_mean, 2),
            'kierunek_3d': [round(float(x), 3) for x in u_3d],
            'wektor_160d': desc160['wektor_160'],
            'tagi': desc160['tagi'],
            'opis': desc160['napis'],
            'podsumowanie': summary,
        })

    return critical_points


def Opanowanie(
    samples: Optional[List[Union[Tuple[int, float], Dict[str, Any]]]] = None,
    L: Optional[int] = None,
    alpha: float = 0.08,
    top_tags: int = 4,
    min_samples: int = 3
) -> Dict[str, Any]:
    """Główna funkcja tworząca sferyczny model kompetencji ucznia w przestrzeni 3D / 160D.

    Wykrywa minima (luki w wiedzy) i maksima (opanowany materiał), podaje białe plamy
    oraz oblicza analityczną niepewność predykcji LOOCV i zapotrzebowanie na próbki.
    """
    pts_3d_list = []
    raw_samples_info = []

    if samples is not None:
        for item in samples:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                tid, sc = int(item[0]), float(item[1])
            elif isinstance(item, dict) and 'id' in item:
                tid = int(item['id'])
                sc = float(item.get('score', item.get('punkty', 10.0)))
            else:
                continue

            if tid in _engine.id_to_idx:
                v160 = _engine.vectors[_engine.id_to_idx[tid]]
                u3d = project_160_to_3d(v160)
                pts_3d_list.append(max(sc, MIN_RADIUS) * u3d)
                raw_samples_info.append({
                    "id": tid,
                    "punkty": sc,
                    "kierunek_3d": [round(float(x), 3) for x in u3d]
                })
    else:
        conn = _engine.get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT h.id, h.result, h.note
                FROM history h
                JOIN (SELECT id, MAX(entry_id) AS max_entry FROM history GROUP BY id) latest
                  ON h.entry_id = latest.max_entry
            """)
            rows = cur.fetchall()
            if not rows:
                cur.execute("SELECT id, result, note FROM done")
                rows = cur.fetchall()
        finally:
            conn.close()

        for r in rows:
            tid = int(r[0])
            res_val = str(r[1])
            note_val = str(r[2]) if r[2] else ""
            sc = _extract_task_score(res_val, note_val)

            if tid in _engine.id_to_idx:
                v160 = _engine.vectors[_engine.id_to_idx[tid]]
                u3d = project_160_to_3d(v160)
                pts_3d_list.append(max(sc, MIN_RADIUS) * u3d)
                raw_samples_info.append({
                    "id": tid,
                    "wynik": res_val,
                    "punkty": sc,
                    "kierunek_3d": [round(float(x), 3) for x in u3d]
                })

    N = len(pts_3d_list)
    if N < min_samples:
        msg = (
            f"Zbyt mała liczba rozwiązanych zadań ({N}/{min_samples}). Do wygenerowania pierwszego zarysu "
            f"kompetencji potrzeba min. 5 zadań, a do stabilnej mapy 15 zadań."
        )
        return {
            "status": "insufficient_samples",
            "liczba_probek": N,
            "wymagane_minimum": min_samples,
            "komunikat": msg,
            "raport_tekstowy": msg,
            "punkty_krytyczne": [],
            "szacunek_niedokladnosci": {
                "liczba_probek": N,
                "wiarygodnosc": "Brak danych (wymagane min. 3 zadania)",
                "opis": msg
            }
        }

    points_array = np.array(pts_3d_list, dtype=np.float32)
    terms, coeffs, stats = fit_spherical_harmonics(points_array, L=L, alpha=alpha)
    critical_points = find_critical_points(terms, coeffs, top_tags=top_tags)

    # Detekcja białych plam (niepokrytych oktantów wiedzy)
    all_octants = [(sx, sy, sz) for sx in [-1.0, 1.0] for sy in [-1.0, 1.0] for sz in [-1.0, 1.0]]
    covered_keys = set()
    for p in points_array:
        covered_keys.add((1.0 if p[0] >= 0 else -1.0, 1.0 if p[1] >= 0 else -1.0, 1.0 if p[2] >= 0 else -1.0))

    untested_spots = []
    for oct_key in all_octants:
        if oct_key not in covered_keys:
            u_untested = np.array(oct_key, dtype=np.float32)
            u_untested /= np.linalg.norm(u_untested)
            tags_info = project_3d_to_160(u_untested, top_k=2)
            untested_spots.append({
                "kierunek_3d": [round(float(x), 2) for x in u_untested],
                "dzialy": tags_info['napis']
            })

    report_lines = [
        "=" * 70,
        "SFERYCZNA MAPA KOMPETENCJI MATURALNYCH (KRAJOBRAZ 3D / R^160)",
        "=" * 70,
        f"Liczba próbek (zadań w historii): {stats['liczba_probek']}",
        f"Dopasowanie harmonik: Stopień L={stats['stopien_L']} (funkcji bazowych: {stats['liczba_funkcji_bazowych']})",
        f"Szacowana niepewność (LOOCV): +/-{stats['margines_bledu']} pkt (RMSE trening: {stats['rmse_trening']}, R^2: {stats['r2']})",
        f"Wiarygodność modelu: {stats['wiarygodnosc']}",
        f"Pokrycie przestrzeni: {stats['pokrycie_sfery']} ({stats['pokryte_oktanty']} sektorów)",
        f"Rekomendacja prób: {stats['rekomendacja']}",
        f">>> UWAGA: Model 3D PCA redukuje R^160 do 3 głównych osi (~{_engine.pca_var_ratio * 100:.0f}% wariancji).",
        "    Służy do orientacji makro-obszarów. Do precyzyjnych mikro-luk użyj Slabe().",
        "-" * 70,
        "WYKRYTE BIEGUNY I PUNKTY KRYTYCZNE (MINIMA / MAKSIMA LOKALNE):",
        "-" * 70,
    ]

    if critical_points:
        for cp in critical_points:
            report_lines.append(f"\n[{cp['typ']}] Wysokość: {cp['wysokosc']:.2f} / 10.0 (odchylenie: {cp['odchylenie_od_sredniej']:+.2f})")
            report_lines.append(f"  Rola: {cp['kategoria']}")
            report_lines.append(f"  Kierunek 3D: [{cp['kierunek_3d'][0]:.2f}, {cp['kierunek_3d'][1]:.2f}, {cp['kierunek_3d'][2]:.2f}]")
            report_lines.append("  Dominujące tagi w R^160:")
            for tag_name, pct in cp['tagi'].items():
                report_lines.append(f"    * {tag_name}: {pct}%")
            if cp.get('rekomendowane_zadanie_id'):
                report_lines.append(f"  -> Sugerowane zadanie do powtórki: ID {cp['rekomendowane_zadanie_id']}")
    else:
        report_lines.append("\n(Brak wyraźnych odchyleń od średniej - profil umiejętności jest równomierny)")

    if untested_spots:
        report_lines.append("\n" + "-" * 70)
        report_lines.append("BIAŁE PLAMY (NIEZBADANE DZIAŁY MATURALNE - BRAK ZADAŃ W DONE):")
        report_lines.append("-" * 70)
        for spot in untested_spots[:3]:
            report_lines.append(f"  * {spot['dzialy']}")

    report_lines.append("\n" + "=" * 70)
    raport_tekstowy = "\n".join(report_lines)

    return {
        "status": "ok",
        "raport_tekstowy": raport_tekstowy,
        "punkty_krytyczne": critical_points,
        "szacunek_niedokladnosci": stats,
        "biale_plamy": untested_spots,
        "probki": raw_samples_info
    }


# ==============================================================================
# ŁATWOŚĆ ZADAŃ (sprawozdania CKE) I JEJ SZACUNEK
# ==============================================================================
#
# Tabela `latwosc` (zbudowana jednorazowo; kod jednorazowy poza repo): łatwość krajowa zadań z matur majowych 2015–2026
# (poziom rozszerzony), czyli średni odsetek punktów zdobytych przez zdających, oraz bliźniaki
# tych zadań z innych zestawów. Dane ma ok. 8% zadań, więc dla pozostałych jest szacunek z modelu
# dopasowanego do zadań z danymi: punkty, pozycja w arkuszu, norma wektora i 7 najbliższych
# zadań z danymi w R^160 (test bez danego zadania: średni błąd 11 pkt proc., sama średnia 16).
# Lata porównujemy względem średniej arkusza danego roku (2023 miał 50%, 2024–2026 33–37%).
# Trudność delta = logit(średnia roku) - logit(łatwość): 0 = przeciętne zadanie rozszerzonej,
# dodatnia = trudniejsze. Brak wektora albo punktów = None: wtedy każde miejsce, które używa
# łatwości, działa jak przed jej wprowadzeniem.

LAT_REF = 34.1        # % średnia arkusza rozszerzonego 2024–2026: punkt odniesienia szacunku
LAT_K = 7             # sąsiedzi w R^160
_lat_cache: Dict[str, Any] = {}


def _logit(p: float) -> float:
    p = min(max(p / 100.0, 0.005), 0.995)
    return float(np.log(p / (1 - p)))


def _lat_tabela() -> Dict[int, Dict[str, Any]]:
    if "tab" not in _lat_cache:
        conn = _engine.get_conn()
        try:
            rows = conn.execute("SELECT id, latwosc, zrodlo, rok, formula, nr, srednia_roku, wzor FROM latwosc").fetchall()
        except sqlite3.OperationalError:
            rows = []  # brak tabeli: wszystko jak przed wprowadzeniem łatwości
        finally:
            conn.close()
        _lat_cache["tab"] = {int(r[0]): dict(r) for r in rows}
    return _lat_cache["tab"]


def _lat_cechy() -> Dict[int, Tuple[Optional[int], float]]:
    """id -> (punkty, pozycja w zestawie 0..1); pozycja = numer zadania z nazwy / największy numer w zestawie."""
    if "cechy" not in _lat_cache:
        conn = _engine.get_conn()
        try:
            rows = conn.execute('SELECT id, name, latex FROM "desc"').fetchall()
        finally:
            conn.close()
        nr, maks = {}, {}
        for i, n, _ in rows:
            r = re.match(r"(.+)_R_ZAD_(\d+)", n or "")
            if r:
                nr[int(i)] = (r.group(1), int(r.group(2)))
                maks[r.group(1)] = max(maks.get(r.group(1), 1), int(r.group(2)))
        _lat_cache["cechy"] = {int(i): (_punkty_z_tekstu(t or ""),
                                        nr[int(i)][1] / maks[nr[int(i)][0]] if int(i) in nr else 0.5)
                               for i, _, t in rows}
    return _lat_cache["cechy"]


def _lat_x(tid: int) -> Optional[np.ndarray]:
    pk, poz = _lat_cechy().get(int(tid), (None, 0.5))
    k = _engine.id_to_idx.get(int(tid))
    if pk is None or k is None or float(_engine.norms[k]) < 0.5:
        return None
    return np.array([1.0, float(pk), float(poz), float(_engine.norms[k])])


def _lat_przewid(mod: Dict[str, Any], x: np.ndarray, u: np.ndarray) -> float:
    s = mod["U"] @ u
    idx = np.argsort(-s)[:LAT_K]
    w = np.clip(s[idx], 0, None) + 1e-6
    return float(x @ mod["b"] + 0.5 * (w * mod["res"][idx]).sum() / w.sum())


def _lat_model() -> Optional[Dict[str, Any]]:
    """Regresja logit(łatwość względem roku) ~ punkty + pozycja + norma, plus poprawka z sąsiadów."""
    if "model" in _lat_cache:
        return _lat_cache["model"]
    ids, X, y = [], [], []
    for i, r in _lat_tabela().items():
        x = _lat_x(i) if r["zrodlo"] == "CKE" else None
        if x is not None:
            ids.append(i)
            X.append(x)
            y.append(_logit(r["latwosc"]) - _logit(r["srednia_roku"]))
    model = None
    if len(ids) >= 30:
        X, y = np.array(X), np.array(y)
        b, *_ = np.linalg.lstsq(X, y, rcond=None)
        U = np.array([_engine.vectors[_engine.id_to_idx[i]] for i in ids], dtype=np.float64)
        U /= np.linalg.norm(U, axis=1, keepdims=True) + 1e-9
        model = {"ids": np.array(ids), "b": b, "U": U, "res": y - X @ b}
        bledy = []  # błąd szacunku w teście bez danego zadania -> szerokość przedziału
        for j in range(len(ids)):
            tr = np.arange(len(ids)) != j
            bj, *_ = np.linalg.lstsq(X[tr], y[tr], rcond=None)
            mj = {"b": bj, "U": U[tr], "res": y[tr] - X[tr] @ bj}
            bledy.append(_lat_przewid(mj, X[j], U[j]) - y[j])
        model["sigma"] = float(np.std(bledy))
        model["mae_pp"] = float(np.mean([abs(100 * _sigm(_logit(LAT_REF) + y[j] + e) - 100 * _sigm(_logit(LAT_REF) + y[j]))
                                         for j, e in enumerate(bledy)]))
    _lat_cache["model"] = model
    return model


def Latwosc(id: int) -> Optional[Dict[str, Any]]:
    """Łatwość krajowa zadania (w %) ze źródłem: 'CKE', 'bliźniak' albo 'szacunek'; None, gdy brak.

    Zwraca latwosc, zrodlo, przedzial (80%), trudnosc (delta: 0 = przeciętne zadanie rozszerzonej,
    więcej = trudniejsze), srednia_roku, a dla danych CKE też rok i nr zadania w arkuszu.
    Przykład: Latwosc(1632) -> {'latwosc': 4.0, 'zrodlo': 'CKE', 'rok': 2018, 'nr': '8', ...}
    """
    tid = int(id)
    r = _lat_tabela().get(tid)
    if r:
        p, sr = float(r["latwosc"]), float(r["srednia_roku"])
        if r["zrodlo"] == "CKE":
            lo, hi = max(p - 3, 0.0), min(p + 3, 100.0)
        else:
            lo, hi = (100 * float(_sigm(_logit(p) + s * _Z80 * 0.5)) for s in (-1, 1))
        return {"latwosc": round(p, 1), "zrodlo": r["zrodlo"], "przedzial": (round(lo), round(hi)),
                "trudnosc": round(_logit(sr) - _logit(p), 3), "srednia_roku": sr,
                "rok": r["rok"], "nr": r["nr"], "wzor": r["wzor"]}
    mod, x = _lat_model(), _lat_x(tid)
    if mod is None or x is None:
        return None
    u = _engine.vectors[_engine.id_to_idx[tid]].astype(np.float64)
    pred = _lat_przewid(mod, x, u / (np.linalg.norm(u) + 1e-9))
    eta = pred + _logit(LAT_REF)
    return {"latwosc": round(100 * float(_sigm(eta)), 1), "zrodlo": "szacunek",
            "przedzial": tuple(round(100 * float(_sigm(eta + s * _Z80 * mod["sigma"]))) for s in (-1, 1)),
            "trudnosc": round(-pred, 3), "srednia_roku": LAT_REF, "rok": None, "nr": None, "wzor": None}


def LatwoscTekst(id: int) -> str:
    """Krótki opis do tabel: 'kraj 22%', 'kraj ≈35%' (szacunek), '' gdy brak."""
    l = Latwosc(id)
    if not l:
        return ""
    return f"kraj {'≈' if l['zrodlo'] == 'szacunek' else ''}{l['latwosc']:.0f}%"


# ==============================================================================
# PROFIL UMIEJĘTNOŚCI (mocne i słabe strony z uwzględnieniem trudności)
# ==============================================================================
#
# Model: szansa rozwiązania zadania i (ułamek punktów y_i) to
#     sigma( theta0 + sum_dzial u_id * g_d + sum_tag w_it * e_t - b * (norma_i - 3.5) )
# gdzie w_it to udział tagu t w wektorze zadania (segment A i B), u_id to udział działu,
# g_d to umiejętność działu, e_t to odchylenie konkretnej umiejętności od działu,
# a b to wpływ trudności. Regresja logistyczna z karą (ridge): g ściągane do 0 słabo,
# e mocniej, więc przy małej liczbie prób oceny są ostrożne, a dział „pożycza” dane od
# swoich tagów. Starsze próby ważą mniej (połowa wagi po POLOWA_DNI dniach). Niepewność
# z przybliżenia Laplace'a (odwrotność hesjanu) daje 80% przedział dla każdej oceny.
#
# Dzięki trudności porażka na bardzo trudnym zadaniu nie robi z działu „słabego”, a sukces
# na łatwym nie robi „mocnego”; dzięki przedziałom dział z jedną próbą trafia do
# „za mało danych”, a nie na szczyt rankingu.

POLOWA_DNI = 30.0
L_DZIAL, L_TAG, L_TRUD = 0.3, 4.0, 4.0  # dobrane na symulowanych uczniach
SREDNIA_NORMA = 3.5
# Trudność zadania w Profilu: "latwosc" = delta z Latwosc (CKE, bliźniak, szacunek; zadanie bez
# łatwości dostaje 0.8 * (norma - 3.5) jak dawniej), "norma" = sama norma wektora (stary model).
# Test na symulowanych uczniach: kod poza repo.
PROFIL_TRUDNOSC = "latwosc"
PROFIL_LAT_REF = 43.0  # % łatwość „zadania średniej trudności” w Profilu: tyle ma typowe zadanie o normie 3.5,
                       # więc opanowanie znaczy to samo co przed łatwością (ciągłość raportów tydzień do tygodnia)
_Z80 = 1.2816


def _sigm(x):
    return 1.0 / (1.0 + np.exp(-x))


def _proby_z_historii() -> List[Tuple[int, float, float]]:
    conn = _engine.get_conn()
    try:
        rows = conn.execute("SELECT id, result, note, timestamp FROM history ORDER BY entry_id").fetchall()
    finally:
        conn.close()
    return [(int(r[0]), _extract_task_score(str(r[1]), r[2] or "") / 10.0, float(r[3])) for r in rows]


def _wiersz(tid: int, lat: bool, tagi: List[int], dzialy: List[str]) -> np.ndarray:
    """Cechy zadania w modelu Profilu: [1, -trudność, udziały działów, udziały tagów]."""
    seg_a = set(_engine.segments_dict.get("A", []))
    nD = len(dzialy)
    x = np.zeros(2 + nD + len(tagi))
    k = _engine.id_to_idx[tid]
    v = _engine.vectors[k]
    x[0] = 1.0
    l = Latwosc(tid) if lat else None
    d_ref = _logit(LAT_REF) - _logit(PROFIL_LAT_REF)
    x[1] = (-(l["trudnosc"] - d_ref) if l
            else -(float(_engine.norms[k]) - SREDNIA_NORMA) * (0.8 if lat else 1.0))
    tw = np.maximum(v[tagi].astype(float), 0.0)
    if tw.sum() > 0:
        tw /= tw.sum()
    x[2 + nD:] = tw
    for t, w in zip(tagi, tw):
        if w > 0 and t in seg_a:
            x[2 + dzialy.index(_engine.dims_list[t]["makro_dzial"])] += w
    s = x[2:2 + nD].sum()
    if s > 0:
        x[2:2 + nD] /= s
    return x


def _dopasuj(proby: List[Tuple[int, float, float]], teraz: Optional[float] = None,
             trudnosc: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Dopasowanie modelu do listy (id zadania, wynik 0..1, timestamp). None, gdy brak prób."""
    lat = (trudnosc or PROFIL_TRUDNOSC) == "latwosc"
    proby = [p for p in proby if p[0] in _engine.id_to_idx and _engine.norms[_engine.id_to_idx[p[0]]] > 0.5]
    if not proby:
        return None
    teraz = teraz or time.time()
    seg_a = set(_engine.segments_dict.get("A", []))
    tagi = sorted(seg_a | set(_engine.segments_dict.get("B", [])))
    dzialy = sorted({_engine.dims_list[t]["makro_dzial"] for t in seg_a})
    nD, nT, n = len(dzialy), len(tagi), len(proby)
    X = np.zeros((n, 2 + nD + nT))
    y = np.zeros(n)
    wt = np.zeros(n)
    for i, (tid, sc, ts) in enumerate(proby):
        X[i] = _wiersz(tid, lat, tagi, dzialy)
        y[i] = min(max(sc, 0.0), 1.0)
        wt[i] = 0.5 ** (max(teraz - ts, 0.0) / 86400.0 / POLOWA_DNI)
    lam = np.r_[1e-3, L_TRUD, np.full(nD, L_DZIAL), np.full(nT, L_TAG)]
    prior = np.zeros(X.shape[1])
    prior[1] = 1.0 if lat else 0.8  # trudność: rozsądny punkt wyjścia; dane mogą go przesunąć
    beta = prior.copy()
    for _ in range(50):  # Newton-Raphson
        p = _sigm(X @ beta)
        g = X.T @ (wt * (p - y)) + lam * (beta - prior)
        H = (X * (wt * p * (1 - p) + 1e-6)[:, None]).T @ X + np.diag(lam)
        krok = np.linalg.solve(H, g)
        beta -= krok
        if np.max(np.abs(krok)) < 1e-6:
            break
    p = _sigm(X @ beta)
    H = (X * (wt * p * (1 - p) + 1e-6)[:, None]).T @ X + np.diag(lam)
    return {"beta": beta, "cov": np.linalg.inv(H), "dzialy": dzialy, "tagi": tagi, "nD": nD,
            "n_d": (X[:, 2:2 + nD] >= 0.3).sum(0), "n_t": (X[:, 2 + nD:] > 0).sum(0), "n": n, "lat": lat}


def _ocena(fit: Dict[str, Any], idx: List[int]) -> Dict[str, Any]:
    """Szansa (0–100%) na komplet w zadaniu średniej trudności + 80% przedział."""
    c = np.zeros(len(fit["beta"]))
    c[0] = 1.0
    c[idx] += 1.0
    eta = float(c @ fit["beta"])
    se = float(np.sqrt(max(c @ fit["cov"] @ c, 0.0)))
    return {"opanowanie": round(100 * _sigm(eta)),
            "przedzial": (round(100 * _sigm(eta - _Z80 * se)), round(100 * _sigm(eta + _Z80 * se)))}


def Profil(prog_mocny: float = 0.75, prog_slaby: float = 0.55, proby=None, teraz: Optional[float] = None,
           trudnosc: Optional[str] = None) -> Dict[str, Any]:
    """Mocne i słabe strony ucznia z historii prób, z uwzględnieniem trudności zadań i niepewności.

    Dla każdego działu i każdej umiejętności (tagu) z prób: „opanowanie” = szansa (w %) na komplet
    punktów w zadaniu średniej trudności (łatwość krajowa 43%, tyle co typowe zadanie o normie 3.5)
    i 80% przedział. Klasyfikacja:
      mocne    – dolna granica przedziału >= prog_mocny, albo ocena >= prog_mocny przy co najmniej
                 3 próbach i dolnej granicy >= prog_slaby,
      slabe    – górna granica < prog_slaby, albo ocena < prog_slaby przy co najmniej 3 próbach,
    (próba działu = zadanie, w którym dział ma co najmniej 30% wagi)
      niepewne – reszta (za mało danych: warto tam dać zadanie, żeby się dowiedzieć).
    Zwraca dzialy, umiejetnosci, mocne, slabe, niepewne, do_sprawdzenia (najszersze przedziały)
    i raport (tekst do ANALIZA.md).

    proby: opcjonalna lista (id, wynik 0..1, timestamp) zamiast historii z bazy (do symulacji).
    """
    proby = _proby_z_historii() if proby is None else proby
    fit = _dopasuj(proby, teraz, trudnosc)
    if fit is None:
        return {"status": "brak_danych", "dzialy": [], "umiejetnosci": [], "mocne": [], "slabe": [],
                "niepewne": [], "do_sprawdzenia": [],
                "raport": "Brak prób w historii: profil powstanie po pierwszych sprawdzonych zadaniach."}
    nD = fit["nD"]
    dz = [{"dzial": d, **_ocena(fit, [2 + k]), "proby": int(fit["n_d"][k])}
          for k, d in enumerate(fit["dzialy"]) if fit["n_d"][k] > 0]
    um = []
    for k, t in enumerate(fit["tagi"]):
        if fit["n_t"][k] == 0:
            continue
        info = _engine.dims_list[t]
        idx = [2 + nD + k] + ([2 + fit["dzialy"].index(info["makro_dzial"])] if info["segment"] == "A" else [])
        um.append({"umiejetnosc": info["tag"], "dzial": info["makro_dzial"], **_ocena(fit, idx), "proby": int(fit["n_t"][k])})

    def klasa(x):
        lo, hi = x["przedzial"]
        if lo >= 100 * prog_mocny or (x["opanowanie"] >= 100 * prog_mocny and lo >= 100 * prog_slaby and x["proby"] >= 3):
            return "mocne"
        if hi < 100 * prog_slaby or (x["opanowanie"] < 100 * prog_slaby and x["proby"] >= 3):
            return "slabe"
        return "niepewne"

    for x in dz + um:
        x["ocena"] = klasa(x)
    dz.sort(key=lambda x: x["opanowanie"])
    um.sort(key=lambda x: x["opanowanie"])
    mocne = [x for x in dz if x["ocena"] == "mocne"][::-1]
    slabe = [x for x in dz if x["ocena"] == "slabe"]
    niepewne = [x for x in dz if x["ocena"] == "niepewne"]
    do_spr = sorted(niepewne, key=lambda x: x["przedzial"][0] - x["przedzial"][1])[:3]
    um_sl = [x for x in um if x["ocena"] == "slabe" and x["proby"] >= 2][:5]
    um_mo = [x for x in um if x["ocena"] == "mocne" and x["proby"] >= 2][::-1][:5]
    b = float(fit["beta"][1])

    def fmt(x):
        return f"{x['dzial']} {x['opanowanie']}% ({x['przedzial'][0]}–{x['przedzial'][1]}, {x['proby']} pr.)"

    lin = [f"Profil z {fit['n']} prób (świeższe ważą więcej; opanowanie = szansa na komplet punktów w zadaniu średniej trudności, w nawiasie 80% przedział).",
           "Mocne: " + ("; ".join(map(fmt, mocne)) if mocne else "jeszcze żadne pewne"),
           "Słabe: " + ("; ".join(map(fmt, slabe)) if slabe else "jeszcze żadne pewne"),
           "Za mało danych: " + ("; ".join(map(fmt, niepewne)) if niepewne else "—")]
    if um_sl:
        lin.append("Najsłabsze umiejętności: " + "; ".join(f"{x['umiejetnosc'][:60]} {x['opanowanie']}%" for x in um_sl))
    if um_mo:
        lin.append("Najmocniejsze umiejętności: " + "; ".join(f"{x['umiejetnosc'][:60]} {x['opanowanie']}%" for x in um_mo))
    if fit["lat"]:
        d20 = _logit(PROFIL_LAT_REF) - _logit(20.0)
        lin.append(f"Trudność: zadanie, które kraj rozwiązuje w 20% zamiast w {PROFIL_LAT_REF:.0f}%, obniża szansę z 50% do {round(100 * _sigm(-b * d20))}%.")
    else:
        lin.append(f"Trudność: zadanie o 1 normę trudniejsze obniża szansę z 50% do {round(100 * _sigm(-b))}%.")
    return {"status": "ok", "dzialy": dz, "umiejetnosci": um, "mocne": mocne, "slabe": slabe, "niepewne": niepewne,
            "do_sprawdzenia": do_spr, "trudnosc_b": round(b, 2), "raport": "\n".join(lin)}


# ==============================================================================
# WAGI DZIAŁÓW NA MATURZE, PROGNOZA WYNIKU, CENTYL KRAJOWY (dane CKE)
# ==============================================================================
#
# Szkielet matury: majowe matury rozszerzone formuły 2023 od WAGA_OD_ROKU (zadania z łatwością CKE
# w tabeli latwosc). Punkty zadania dzielą się na działy według wektora (segment A), więc
# WagaMatury() = średnia liczba punktów działu na arkuszu 50 pkt. Dział, którego na tych maturach
# nie było, dostaje WAGA_DOMYSLNA; bez tabeli latwosc wszystkie działy mają wagę 1 (jak przed wagami).
# Prognoza() = oczekiwany wynik na tych maturach z modelu Profilu; Centyl() = odsetek zdających
# z niższym wynikiem (rozkład beta ze średniej i odchylenia ze sprawozdań CKE).
# Testy na symulowanych uczniach: kod poza repo.

WAGA_OD_ROKU = 2023
WAGA_DOMYSLNA = 0.5  # pkt na arkusz dla działu, którego nie było na maturach od WAGA_OD_ROKU
# Obrazy 12.1 z matur 2025 i 2026 pokazują treść 12.2 (README, „Łatwość CKE”): ich wektor policzyłby
# pochodną dwa razy, więc te rekordy nie wchodzą do wag ani prognozy.
_POMIN_W_WAGACH = {229, 58}
# Wynik kraju na rozszerzonej (formuła 2023, w % punktów): rok -> (średnia, odchylenie), sprawozdania CKE.
ROZKLAD_KRAJU = {2025: (33.0, 26.0), 2026: (37.0, 28.0)}


def _udzialy_dzialow(tid: int) -> Dict[str, float]:
    v = _engine.vectors[_engine.id_to_idx[tid]]
    s: Dict[str, float] = {}
    for d in _engine.dims_list:
        if d.get("segment") == "A" and v[d["id"]] > 0:
            s[d["makro_dzial"]] = s.get(d["makro_dzial"], 0.0) + float(v[d["id"]])
    suma = sum(s.values())
    return {k: w / suma for k, w in s.items()} if suma else {}


def _matury_cke() -> Dict[int, List[Tuple[int, int]]]:
    """rok -> [(id, punkty)] zadań majowych matur formuły 2023 z łatwością CKE."""
    if "matury" not in _lat_cache:
        out: Dict[int, List[Tuple[int, int]]] = {}
        for i, r in _lat_tabela().items():
            if (r["zrodlo"] == "CKE" and str(r["formula"]) == "2023" and (r["rok"] or 0) >= WAGA_OD_ROKU
                    and i not in _POMIN_W_WAGACH and i in _engine.id_to_idx and float(_engine.norms[_engine.id_to_idx[i]]) > 0.5):
                pk = Punkty(i)
                if pk:
                    out.setdefault(int(r["rok"]), []).append((i, pk))
        _lat_cache["matury"] = out
    return _lat_cache["matury"]


def WagaMatury(dzial: Optional[str] = None) -> Any:
    """Średnia liczba punktów działu (makro_dzial) na maturze rozszerzonej 50 pkt.

    WagaMatury() -> {dział: pkt}; WagaMatury('Planimetria') -> 8.7. Dział bez punktów na maturach
    od WAGA_OD_ROKU -> WAGA_DOMYSLNA; brak danych CKE -> 1.0 dla każdego działu (wszystkie równe).
    """
    if "wagi" not in _lat_cache:
        lata = _matury_cke()
        w: Dict[str, float] = {}
        for rok, zad in lata.items():
            suma = sum(pk for _, pk in zad)
            for i, pk in zad:
                for d, u in _udzialy_dzialow(i).items():
                    w[d] = w.get(d, 0.0) + 50.0 * pk / suma * u / len(lata)
        _lat_cache["wagi"] = {d: round(x, 2) for d, x in w.items()}
    wagi = _lat_cache["wagi"]
    if dzial is None:
        return dict(sorted(wagi.items(), key=lambda x: -x[1]))
    if not wagi:
        return 1.0
    return max(wagi.get(dzial, 0.0), WAGA_DOMYSLNA)


def _beta_cdf(x: float, a: float, b: float) -> float:
    """Dystrybuanta rozkładu beta bez scipy (podstawienie t = u^(1/a), potem trapezy)."""
    x = min(max(x, 0.0), 1.0)
    u = np.linspace(0.0, 1.0, 4001)
    f = (1.0 - u ** (1.0 / a)) ** (b - 1.0)
    cal = np.concatenate([[0.0], np.cumsum((f[1:] + f[:-1]) / 2 * np.diff(u))])
    return float(np.interp(x ** a, u, cal) / cal[-1])


def Centyl(procent: float, rok: Optional[int] = None) -> Optional[int]:
    """Odsetek zdających rozszerzoną (formuła 2023) z wynikiem niższym niż `procent`.

    Rozkład beta o średniej i odchyleniu ze sprawozdania CKE (mediana zgadza się z CKE co do 1–2 pkt);
    bez roku: średnia z lat w ROZKLAD_KRAJU. None, gdy brak danych dla roku."""
    lata = [rok] if rok else list(ROZKLAD_KRAJU)
    wyn = []
    for r in lata:
        if r not in ROZKLAD_KRAJU:
            return None
        mu, sd = (x / 100 for x in ROZKLAD_KRAJU[r])
        k = mu * (1 - mu) / sd ** 2 - 1
        wyn.append(_beta_cdf(procent / 100, mu * k, (1 - mu) * k))
    return round(100 * float(np.mean(wyn)))


def Prognoza(proby=None, teraz: Optional[float] = None, n_los: int = 400) -> Dict[str, Any]:
    """Prognoza wyniku matury: oczekiwany % punktów na majowych maturach od WAGA_OD_ROKU z modelu Profilu.

    Szansa w zadaniu = model Profilu (działy, tagi, trudność z łatwości CKE), punkty = szansa x punkty
    zadania. Przedział 80% z losowania parametrów modelu (w symulacji raczej za szeroki niż za wąski).
    Test (skrypty w kopii przed iteracją, statystyki_cke/analiza_wagi): błąd ok. 3 pkt proc. przy 36 próbach, 2 przy 144.
    Zwraca status, arkusze {rok: (proc, lo, hi)}, prognoza, przedzial, centyl, proby i tekst."""
    proby = _proby_z_historii() if proby is None else proby
    lata = _matury_cke()
    fit = _dopasuj(proby, teraz) if lata else None
    if fit is None:
        return {"status": "brak_danych", "tekst": ""}
    rng = np.random.default_rng(0)
    B = rng.multivariate_normal(fit["beta"], fit["cov"], n_los)
    ark, los = {}, []
    for rok, zad in sorted(lata.items()):
        X = np.array([_wiersz(i, fit["lat"], fit["tagi"], fit["dzialy"]) for i, _ in zad])
        pk = np.array([p for _, p in zad], dtype=float)
        s = 100 * (_sigm(B @ X.T) * pk).sum(1) / pk.sum()
        los.append(s)
        ark[rok] = (round(float(100 * (_sigm(X @ fit["beta"]) * pk).sum() / pk.sum())),
                    round(float(np.percentile(s, 10))), round(float(np.percentile(s, 90))))
    sr = np.mean(los, axis=0)
    prog = round(float(np.mean([v[0] for v in ark.values()])))
    prz = (round(float(np.percentile(sr, 10))), round(float(np.percentile(sr, 90))))
    c = Centyl(prog)
    tekst = (f"Prognoza matury z {fit['n']} prób: {prog}% (80% przedział {prz[0]}–{prz[1]}%; "
             + ", ".join(f"{r}: {v[0]}%" for r, v in ark.items()) + ")"
             + (f", czyli lepiej niż ok. {c}% zdających" if c is not None else "") + ".")
    return {"status": "ok", "arkusze": ark, "prognoza": prog, "przedzial": prz, "centyl": c,
            "proby": fit["n"], "tekst": tekst}


Landscape = Opanowanie
AnalyzeMasteryLandscape = Opanowanie
CriticalDirections = Opanowanie


# ==============================================================================
# DEFINICJE NARZĘDZI DLA AGENTA (MCP / FUNCTION CALLING)
# ==============================================================================

def get_agent_tools() -> List[Dict[str, Any]]:
    """Zwraca zwięzły zestaw narzędzi w formacie JSON Schema dla Claude'a i systemów Tool Use."""
    tools = [
        {
            "name": "Znajdz",
            "description": "Wyszukuje niezrobione zadania najbliższe zadanemu tematowi (wektorowi) i docelowej trudności (length: 1.5 - 6.0+).",
            "parameters": {
                "type": "object",
                "properties": {
                    "vector": {
                        "type": "object",
                        "description": "Słownik rzadki np. {'ciąg geometryczny': 3.0} lub lista 160 floatów."
                    },
                    "length": {
                        "type": "number",
                        "description": "Poziom trudności (norma wektora od 1.5 do 6.0+). Pomiń, by użyć normy z vector."
                    },
                    "samples": {"type": "integer", "description": "Liczba zadań do zwrócenia (domyślnie 1).", "default": 1},
                    "exclude": {"type": "array", "items": {"type": "integer"}, "description": "ID zadań do wykluczenia."},
                    "level": {"type": "string", "enum": ["basic", "extended"], "description": "Poziom matury (podstawowy/rozszerzony)."}
                },
                "required": ["vector"]
            }
        },
        {
            "name": "Pobierz",
            "description": "Zwraca treść matematyczną LaTeX lub link do zdjęcia dla podanych ID zadań.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "integer"}, "description": "Lista ID zadań do pobrania."},
                    "type": {"type": "string", "enum": ["text", "photos", "both"], "default": "text"}
                },
                "required": ["ids"]
            }
        },
        {
            "name": "Odnotuj",
            "description": "Zapisuje wynik rozwiązania zadania przez ucznia ('ok', 'partial', 'fail') oraz opcjonalną notatkę o błędzie.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "ID zadania."},
                    "result": {"type": "string", "enum": ["ok", "partial", "fail"], "description": "'ok'=samodzielnie, 'partial'=z pomocą, 'fail'=nieudane."},
                    "note": {"type": "string", "description": "Opcjonalna notatka o napotkanym problemie, np. 'pomylił wzór na deltę'."}
                },
                "required": ["id", "result"]
            }
        },
        {
            "name": "Ostatnie",
            "description": "Zwraca pamięć ucznia: ostatnie wykonane próby z wynikami i notatkami o błędach.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "Liczba ostatnich prób do pobrania (domyślnie 20).", "default": 20}
                }
            }
        },
        {
            "name": "Wymiary",
            "description": "Zwraca słownik 160 wymiarów/tagów maturalnych (nazwa -> indeks) pomocny przy budowaniu wektorów.",
            "parameters": {
                "type": "object",
                "properties": {
                    "segment": {"type": "string", "enum": ["A", "B", "C"], "description": "Segment: 'A'=pojęcia, 'B'=mikro-narzędzia, 'C'=styl poznawczy."}
                }
            }
        },
        {
            "name": "ZadaniaPodobne",
            "description": "Wyszukuje zadania powiązane z danym zadaniem ('twin'=bliźniak, 'harder'=trudniejsze, 'easier'=łatwiejsze, 'same_tools'=te same techniki).",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "ID zadania referencyjnego."},
                    "samples": {"type": "integer", "description": "Liczba zadań.", "default": 1},
                    "mode": {"type": "string", "enum": ["twin", "harder", "easier", "same_tools"], "default": "twin"}
                },
                "required": ["id"]
            }
        },
        {
            "name": "Slabe",
            "description": "Zwraca tematy, w których uczeń ma najwięcej błędów lub najniższą skuteczność na podstawie historii.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Liczba słabych tematów (domyślnie 5).", "default": 5},
                    "min_attempts": {"type": "integer", "description": "Minimalna liczba prób na dany temat (domyślnie 1).", "default": 1}
                }
            }
        },
        {
            "name": "Statystyki",
            "description": "Zwraca ogólne statystyki zrobionych i pozostałych zadań.",
            "parameters": {"type": "object", "properties": {}}
        },
        {
            "name": "Opanowanie",
            "description": "Sferyczny model kompetencji 3D / R^160 z harmonik sferycznych: wyznacza minima (luki w wiedzy) i maksima (opanowany materiał), podaje stopień udziału tagów i szacunek niepewności.",
            "parameters": {
                "type": "object",
                "properties": {
                    "L": {"type": "integer", "description": "Stopień harmonik sferycznych (1-4). Domyślnie None (dobierany automatycznie do liczby próbek)."},
                    "alpha": {"type": "number", "description": "Współczynnik regularyzacji Ridge (domyślnie 0.08).", "default": 0.08},
                    "top_tags": {"type": "integer", "description": "Liczba dominujących tagów dla każdego punktu krytycznego (domyślnie 4).", "default": 4}
                }
            }
        }
    ]

    # Zapewniamy zgodność zarówno ze standardem OpenAI/Gemini (parameters),
    # jak i standardem Claude Tool Use (input_schema).
    for t in tools:
        t["input_schema"] = t["parameters"]

    return tools


def call_tool(name: str, arguments: Dict[str, Any]) -> Any:
    """Dyspozytor narzędzi dla agenta AI."""
    tool_map = {
        "Znajdz": Znajdz,
        "Pobierz": Pobierz,
        "Odnotuj": Odnotuj,
        "Ostatnie": Ostatnie,
        "Wymiary": Wymiary,
        "ZadaniaPodobne": ZadaniaPodobne,
        "Slabe": Slabe,
        "Profil": Profil,
        "Statystyki": Statystyki,
        "Wektor": Wektor,
        "Info": Info,
        "Punkty": Punkty,
        "Opanowanie": Opanowanie,
        "Landscape": Opanowanie,
        "project_160_to_3d": project_160_to_3d,
        "project_3d_to_160": project_3d_to_160,
    }
    if name not in tool_map:
        raise MaturaError(f"Nieznane narzędzie '{name}'.")
    return tool_map[name](**arguments)


# ==============================================================================
# SEKCJA: plan (dawniej plan.py)
# ==============================================================================

# Warstwa planu nad matura.py: harmonogram, rezerwa pełnych arkuszy, powtórki w odstępach, punkty.
#
# Start:
#     import sys; sys.path.insert(0, "<folder repo>"); import matura as m, plan as p
#
# Najważniejsze:
#     p.Propozycja("2026-10-20", "14:00")  # gotowy plan arkusza: role, klucze, length, po 4 kandydatów na slot
#     p.Raport()                  # dane do analizy postępów w jednym tekście (do ANALIZA.md)
#     p.Tydzien()                 # bieżący tydzień harmonogramu (faza, blok, klucze Znajdz, length, pełny arkusz)
#     p.Szukaj(spec, length, 4)   # jak m.Znajdz, ale bez rezerwy pełnych arkuszy (i ich wariantów), zadań sprzed 2015,
#                                 # zamkniętych, śmieci OCR, odrzuconych, duplikatów zrobionych i zadań z niesprawdzonych arkuszy
#     p.Szukaj(spec, length, 4, latwosc=(0, 30))  # najpierw zadania o łatwości krajowej 0–30% (m.Latwosc), potem reszta
#     p.NaTleKraju()              # Twój wynik wobec przeciętnego maturzysty: działy, zadania trudne dla kraju
#     p.Powtorki()                # zadania z błędem, którym minął odstęp (3/7/21 dni): dla każdego bliźniak do arkusza;
#                                 # kolejność według punktów do odzyskania (waga działu na maturze, łatwość)
#     p.Pewniaki()                # typy zadań z każdej matury 2023–2026 i ile ich już było w historii
#     p.Odrzuc(id, powod)         # zadanie obejrzane i złe (fragment, ucięte, brak rysunku): nie wróci w Szukaj
#     p.Zestaw("CKE_2025_Z01")    # id zadań pełnego arkusza po kolei, z punktami
#     p.Podzial("CKE_2025_Z01")   # pełny arkusz na dwie lekcje (ok. 25 pkt każda)
#     p.Punkty(id)                # maksymalna liczba punktów zadania, jeśli da się odczytać
#     p.Zapisz(id, pkt, maks, typ, opis, powt=None)  # Odnotuj z oceną z punktów i notatką w stałym formacie
#     p.Wynik(folder)             # WYNIK.md arkusza z zapisanych prób (po sprawdzeniu)
#     p.Pokrycie()                # próby i średni wynik w każdym dziale
#     p.Niesprawdzone()           # arkusze, których zadania nie mają jeszcze wpisu w historii
#     p.Przeglad()                # tempo z 14 dni do przeglądu planu (PROCEDURY.md#przeglad)
#     p.Wydluz(tydz, powod)       # blok o tydzień dłużej, faza arkuszy krótsza (min. 4 tygodnie)
#     p.Skroc(tydz, powod)        # opanowany blok wypada, faza arkuszy dłuższa




HARMONOGRAM = DANE / "harmonogram.json"
ARKUSZE = BASE / "arkusze"
WADLIWE = DANE / "wadliwe_obrazy.json"
ODRZUCONE = DANE / "odrzucone.json"

# Odstępy powtórek: po fail bliźniak po 3 dniach, po partial po 7, po udanej powtórce kolejna po 21.
ODSTEP_FAIL, ODSTEP_PARTIAL, ODSTEP_UTRWALENIE = 3, 7, 21
POWT = re.compile(r"powt:(\d+)")

# Wagi CKE (README, „Wagi CKE”): +1 zadanie po bloku tylko dla działu wartego tyle pkt na maturze.
WAGA_PLUS1 = 2.0
# Pewniaki: typy zadań obecne na każdej z matur 2023–2026 (sprawozdania CKE). Klucze jak w harmonogramie
# (m.Wektor); próba się liczy, gdy te wymiary mają co najmniej 30% wagi działów zadania.
PEWNIAKI = [
    ("Równanie trygonometryczne", ["Równania trygonometryczne"], "3–5 pkt"),
    ("Optymalizacja z pochodną", ["Zagadnienia optymalizacyjne"], "4 pkt"),
    ("Planimetria: dowód, podobieństwo, okręgi", ["Trójkąty: suma kątów", "Okrąg i koło", "Czworokąt wpisany", "Twierdzenie o dwusiecznej"], "3–6 pkt"),
    ("Stereometria", ["Stereometria"], "4–7 pkt"),
    ("Geometria analityczna: wektory, okręgi", ["Wektory na płaszczyźnie", "Wzajemne położenie prostej i okręgu"], "3–5 pkt"),
    ("Ciąg lub szereg geometryczny", ["Ciągi liczbowe"], "4 pkt"),
    ("Kombinatoryka i prawdopodobieństwo", ["Kombinatoryka", "Klasyczna definicja prawdopodobieństwa", "Prawdopodobieństwo warunkowe"], "3–6 pkt"),
    ("Parametr, wzory Viète'a", ["Wzory Viete'a w zadaniach z parametrem", "Równania kwadratowe z parametrem"], "5–6 pkt"),
    ("Równanie lub nierówność z modułem", ["Równania z wartością bezwzględną", "Nierówności z wartością bezwzględną"], "4 pkt"),
]
MIN_PROB_PEWNIAKA = 3


def _strata(x: dict) -> float:
    """Punkty matury tracone w dziale (wpis z m.Profil): waga CKE x (1 - opanowanie)."""
    return WagaMatury(x["dzial"]) * (1 - x["opanowanie"] / 100)
DNI = ("pon", "wt", "sr", "czw", "pt", "sob", "nd")

# Rekordy, które nie są treścią zadania: schemat oceniania, wstęp strony, ścinki.
_SMIEC = re.compile(
    r"(?i)rozwiązanie pełne|zdający otrzymuje|zdający\s+\S+\s+\S+|schemat (punktowania|oceniania)|wykonana czynność"
    r"|zasadnicz\S* trudno|istotny postęp|uwaga\s+je[żŜ]eli|wiadomości i rozumienie|w każdym z zadań od|europe"
    r"|egzaminator|przypadk\S*:|rozwiązania zadań|zapisanie\s|obliczenie\s|wyznaczenie\s"
    r"|korzystam|podstawiaj\S*|otrzymujemy|rozwiązanie\s*:|odpowied[źz]\s*:"
)

_e = _engine


def _con() -> sqlite3.Connection:
    return sqlite3.connect(str(BASE / "matura.db"), timeout=30)


@lru_cache(maxsize=1)
def _teksty() -> tuple[dict[int, str], dict[int, str]]:
    with _con() as c:
        rows = c.execute('SELECT id, name, latex FROM "desc"').fetchall()
    return {r[0]: r[1] for r in rows}, {r[0]: r[2] or "" for r in rows}


def _nazwy() -> dict[int, str]:
    return _teksty()[0]


def _rok(nazwa: str) -> int:
    r = re.search(r"_(\d{4})_", nazwa)
    return int(r.group(1)) if r else 0


def _dzien(d: date | str | None) -> date:
    return date.fromisoformat(d) if isinstance(d, str) else (d or Dzis())


def Harmonogram() -> dict:
    return json.loads(HARMONOGRAM.read_text(encoding="utf-8"))


def Tydzien(dzien: date | str | None = None) -> dict:
    """Wpis harmonogramu dla danego dnia (domyślnie dziś w Polsce) + skład arkusza dla fazy."""
    h = Harmonogram()
    d = _dzien(dzien)
    for t in h["tygodnie"]:
        if date.fromisoformat(t["od"]) <= d <= date.fromisoformat(t["do"]):
            return {**t, "sklad": h["sklad_arkusza"][t["faza"]], "lekcja": h["lekcja"]}
    if d < date.fromisoformat(h["tygodnie"][0]["od"]):
        t = h["tygodnie"][0]
    else:
        t = h["tygodnie"][-1]
    return {**t, "sklad": h["sklad_arkusza"][t["faza"]], "lekcja": h["lekcja"], "poza_harmonogramem": True}


# ---------------------------------------------------------------- wykluczenia

def ZestawyCKE() -> dict:
    """Prawdziwe matury majowe w rezerwie (harmonogram → zestawy_cke): nazwa → ids po kolei i wyniki kraju.
    Ich rekordy nie mają wspólnego przedrostka (w bazie leżą obok arkusza formuły 2015), stąd jawna lista."""
    return Harmonogram().get("zestawy_cke", {})


def Rezerwa() -> set[int]:
    """Id wszystkich rekordów pełnych arkuszy zarezerwowanych na symulacje (nie trafiają do codziennych arkuszy)."""
    h = Harmonogram()
    cke = h.get("zestawy_cke", {})
    zestawy = tuple(z for z in h["rezerwa_pelnych_arkuszy"] if z not in cke)
    out = {i for i, n in _nazwy().items() if n.startswith(zestawy)}
    ids = {int(i) for z in h["rezerwa_pelnych_arkuszy"] if z in cke for i in cke[z]["ids"] + cke[z].get("wyklucz", [])}
    # bliźniaki z tabeli latwosc (ten sam tekst w arkuszu formuły 2015 albo w innym zestawie)
    return out | ids | {i for i, r in _lat_tabela().items() if r["wzor"] in ids}


def _podobne() -> list[tuple[int, int, str]]:
    """Pary z tabeli podobne (zbudowana jednorazowo; kod w kopii przed iteracją); pusta lista, gdy indeksu jeszcze nie ma."""
    try:
        with _con() as c:
            return c.execute("SELECT id1, id2, typ FROM podobne").fetchall()
    except sqlite3.OperationalError:
        return []


def Podobne(ids, tylko_duplikaty: bool = False) -> set[int]:
    """Zadania, które są duplikatem (ta sama treść) lub wariantem (inne liczby) zadań z `ids`."""
    ids = {int(i) for i in ids}
    out = set()
    for a, b, typ in _podobne():
        if tylko_duplikaty and typ != "duplikat":
            continue
        if a in ids:
            out.add(b)
        if b in ids:
            out.add(a)
    return out - ids


def _puste() -> set[int]:
    try:
        w = json.loads(WADLIWE.read_text(encoding="utf-8")).get("wadliwe", {})
        return {int(k) for k, v in w.items() if "pusty" in str(v)}
    except Exception:
        return set()


def _wadliwe_krawedz() -> set[int]:
    """Kadry z tuszem przy krawędzi (dol/gora): nadają się po obejrzeniu, ale wolimy czyste."""
    try:
        w = json.loads(WADLIWE.read_text(encoding="utf-8")).get("wadliwe", {})
        return {int(k) for k, v in w.items() if "pusty" not in str(v)}
    except Exception:
        return set()


@lru_cache(maxsize=1)
def Smieci() -> frozenset[int]:
    """Rekordy, które nie są treścią zadania (schemat oceniania, wstęp strony, ścinek) albo nie mają wektora."""
    out = set()
    for i, t in _teksty()[1].items():
        glowa = re.sub(r"(?i)strona \d+ z \d+|\S*MAP R0 100|^\W*\d+\s", "", t).strip()[:110]
        if _SMIEC.search(glowa) or len(re.sub(r"\s", "", t)) < 30:
            out.add(i)
    out |= {int(i) for i, n in zip(_e.ids, _e.norms) if n < 0.5}
    return frozenset(out)


def Odrzucone() -> dict[int, str]:
    try:
        return {int(k): v for k, v in json.loads(ODRZUCONE.read_text(encoding="utf-8")).items()}
    except FileNotFoundError:
        return {}


def Odrzuc(id: int, powod: str) -> None:
    """Zapamiętuje zadanie obejrzane i nieużywalne (fragment rozwiązania, ucięta treść, brak rysunku,
    treść spoza wymagań). Szukaj, Blizniak i Propozycja więcej go nie podadzą."""
    o = Odrzucone()
    o[int(id)] = f"{Dzis():%Y-%m-%d} {powod}"
    ODRZUCONE.write_text(json.dumps({str(k): v for k, v in sorted(o.items())}, ensure_ascii=False, indent=1), encoding="utf-8")


def _start_arkusza(folder: Path) -> float:
    """Od kiedy liczą się próby tego arkusza (timestamp). Z nazwy folderu RRRR-MM-DD_GGMM: 3 h przed lekcją
    (arkusz powstaje godzinę przed, z zapasem). Dawniej mtime zadania.json, co psuło się po kopiowaniu i
    po zapisie pliku po złożeniu arkusza; mtime zostaje tylko jako zapas dla nazw spoza wzoru."""
    mt = re.match(r"(\d{4})-(\d\d)-(\d\d)_(\d\d)(\d\d)$", Path(folder).name)
    if mt:
        y, mo, d, h, mi = map(int, mt.groups())
        return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp() - 3 * 3600
    return (Path(folder) / "zadania.json").stat().st_mtime - 3600


def _arkusze() -> list[tuple[str, dict]]:
    """(nazwa folderu, zadania.json) dla wszystkich prawdziwych arkuszy, chronologicznie."""
    out = []
    for z in sorted(ARKUSZE.glob("*/zadania.json")):
        if z.parent.name.endswith("_przyklad"):
            continue
        try:
            out.append((z.parent.name, json.loads(z.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            continue
    return out


def Niesprawdzone() -> dict[str, list[int]]:
    """Folder arkusza -> id zadań z zadania.json, które nie mają jeszcze żadnego wpisu w historii."""
    with _con() as c:
        w_historii = {r[0] for r in c.execute("SELECT DISTINCT id FROM history")}
    wynik = {}
    for nazwa, z in _arkusze():
        ids = [t["id"] for t in z.get("zadania", [])]
        brak = [i for i in ids if i not in w_historii]
        if brak:
            wynik[nazwa] = brak
    return wynik


def Wykluczone(od_roku: int = 2015) -> set[int]:
    """Wszystko, czego nie dajemy w codziennym arkuszu:
    rezerwa pełnych arkuszy i jej warianty z innych zestawów, zadania sprzed od_roku, puste kadry,
    śmieci OCR, odrzucone po obejrzeniu, zadania z niesprawdzonych arkuszy, zadania już zrobione
    (także po fail: powtórka idzie przez bliźniaka z m.Powtorki/m.Blizniak, nie przez oryginał)
    oraz duplikaty zadań zrobionych lub czekających na sprawdzenie."""
    rez = Rezerwa()
    stare = {i for i, n in _nazwy().items() if _rok(n) < od_roku}
    wiszace = {i for ids in Niesprawdzone().values() for i in ids}
    with _con() as c:
        zrobione = {r[0] for r in c.execute("SELECT DISTINCT id FROM history")}
    return (rez | Podobne(rez) | stare | _puste() | set(Smieci()) | set(Odrzucone()) | wiszace | zrobione
            | Podobne(zrobione | wiszace, tylko_duplikaty=True))


def Punkty_plan(id: int) -> int | None:
    """Maks. punkty z treści: 'ZADANIE 4 (5 PKT)', 'Zadanie 4. (0–5)', '(4 p.)' itd."""
    return _punkty_z_tekstu(_teksty()[1].get(int(id), ""))


def Szukaj(spec, length: float | None = None, samples: int = 4, od_roku: int = 2015, exclude=(),
           otwarte: bool = True, czyste: bool = True, latwosc: tuple | None = None, **kw) -> list[int]:
    """m.Znajdz z wykluczeniami (Wykluczone). spec: dict jak w Znajdz albo wektor.

    latwosc=(od, do) w %: najpierw zadania, których łatwość krajowa (m.Latwosc: CKE, bliźniak albo
    szacunek) mieści się w zakresie; zadania bez łatwości i spoza zakresu dopełniają listę, gdy
    w zakresie zabraknie kandydatów. Bez latwosc działa jak dawniej.

    otwarte=True pomija zadania zamknięte (A–D albo 1 pkt): na maturze od 2023 wszystkie są otwarte.
    Pomija też rekordy bez punktów w nagłówku: to prawie zawsze fragmenty rozwiązań, nie zadania.
    czyste=True najpierw szuka wśród kadrów bez tuszu przy krawędzi (mniej odrzutów w matura.py arkusz),
    a dopiero gdy ich brakuje, bierze pozostałe.
    """
    if isinstance(spec, str):  # m.Szukaj("Granica ciągu") = {"Granica ciągu": 3}, jak klucze w harmonogramie
        spec = {spec: 3}
    ex = set(exclude) | Wykluczone(od_roku)
    out: list[int] = []
    zapas: list[int] = []  # poza zakresem łatwości: tylko gdy w zakresie zabraknie kandydatów

    def w_zakresie(i: int) -> bool:
        if latwosc is None:
            return True
        l = Latwosc(i)
        return l is None or latwosc[0] <= l["latwosc"] <= latwosc[1]
    for brudne in ([False, True] if czyste else [True]):
        ex2 = set(ex) | (set() if brudne else _wadliwe_krawedz())
        for _ in range(4):
            found = Znajdz(spec, length=length, samples=samples * 3, exclude=list(ex2), **kw)
            if not found:
                break
            for i in found:
                ex2.add(i)
                ex.add(i)
                if not otwarte or (not Zamkniete(i) and Punkty_plan(i) is not None):
                    (out if w_zakresie(i) else zapas).append(i)
            if len(out) >= samples:
                return out[:samples]
    return (out + zapas)[:samples]


def Blizniak(id: int, samples: int = 3, tryb: str = "twin", exclude=()) -> list[int]:
    """Jak m.ZadaniaPodobne, ale z tymi samymi wykluczeniami co Szukaj. tryb: twin | harder | easier."""
    idx = _e.id_to_idx[int(id)]
    vec, norm = _e.vectors[idx], float(_e.norms[idx])
    dl = {"twin": norm, "harder": norm * 1.3, "easier": max(1.5, norm * 0.75)}[tryb]
    return Szukaj(vec, length=dl, samples=samples, exclude=set(exclude) | {int(id)} | Podobne([id]))


def Powtorki(dzis: date | None = None) -> list[dict]:
    """Kolejka powtórek w odstępach.

    Łańcuch zaczyna się od zadania z wynikiem fail/partial. Powtórkę zapisuje się jako
    Odnotuj(bliźniak, ..., note='...; powt:<id pierwotnego>'). Udana powtórka przesuwa następną
    o 21 dni, dwie udane zamykają łańcuch, nieudana zaczyna liczenie od nowa.

    Kolejność (ważna, bo arkusz bierze pierwsze 2–3): punkty do odzyskania = waga działu na maturze
    (m.WagaMatury) x (0.5 + łatwość krajowa zadania), razy (1 + dni zaległości / 7). Najpierw
    działy, które dużo ważą na maturze, i zadania łatwe dla kraju; mocno zaległe i tak awansują.
    """
    dzis = dzis or Dzis()
    with _con() as c:
        rows = c.execute("SELECT id, result, note, timestamp FROM history ORDER BY entry_id").fetchall()
    lancuchy: dict[int, dict] = {}
    for tid, res, note, ts in rows:
        d = _data(ts).date()
        p = POWT.search(note or "")
        if p:
            k = int(p.group(1))
            if k not in lancuchy:
                continue
            l = lancuchy[k]
            l["ostatnie"] = tid
            if res == "ok":
                l["udane"] += 1
                l["termin"] = d.toordinal() + ODSTEP_UTRWALENIE
            else:
                l["udane"] = 0
                l["termin"] = d.toordinal() + (ODSTEP_FAIL if res == "fail" else ODSTEP_PARTIAL)
        elif res in ("fail", "partial"):
            lancuchy[tid] = {"id": tid, "ostatnie": tid, "udane": 0, "notatka": note or "",
                             "termin": d.toordinal() + (ODSTEP_FAIL if res == "fail" else ODSTEP_PARTIAL)}
        elif tid in lancuchy:
            # To samo zadanie później zrobione dobrze (poprawa) zamyka łańcuch.
            lancuchy[tid]["udane"] = 2
    nazwy = _nazwy()
    due = []
    for k, l in lancuchy.items():
        if l["udane"] >= 2 or l["termin"] > dzis.toordinal():
            continue
        zalegle = dzis.toordinal() - l["termin"]
        lat = Latwosc(k)
        waga = WagaMatury(Dzial(k))
        due.append({"pierwotne": k, "nazwa": nazwy.get(k, ""), "notatka": l["notatka"],
                    "ostatnie": l["ostatnie"], "zalegle_dni": zalegle, "udane_powtorki": l["udane"],
                    "priorytet": round(waga * (0.5 + (lat["latwosc"] if lat else 40.0) / 100) * (1 + zalegle / 7), 2)})
    return sorted(due, key=lambda x: (-x["priorytet"], -x["zalegle_dni"]))


def Zestaw(nazwa: str) -> list[dict]:
    """Pełny arkusz (np. 'CKE_2025_Z01' albo matura 'CKE_2025_05'): zadania po kolei z punktami. Suma 50."""
    cke = ZestawyCKE().get(nazwa)
    if cke:
        lat = _lat_tabela()
        out = []
        for i in cke["ids"]:
            nr = str((lat.get(int(i)) or {}).get("nr") or "0")
            out.append({"id": int(i), "nazwa": _nazwy().get(int(i), ""), "nr": int(nr.split(".")[0]),
                        "czesc": int(nr.split(".")[1]) if "." in nr else 0, "pkt": Punkty_plan(int(i))})
        return out
    out = []
    for i, n in _nazwy().items():
        r = re.match(re.escape(nazwa) + r"_R_ZAD_(\d+)(?:_(\d+))?$", n)
        if r:
            out.append({"id": i, "nazwa": n, "nr": int(r.group(1)), "czesc": int(r.group(2) or 0), "pkt": Punkty_plan(i)})
    return sorted(out, key=lambda x: (x["nr"], x["czesc"]))


def Podzial(nazwa: str) -> dict:
    """Pełny arkusz na dwie lekcje: część 1 to kolejne zadania do ok. połowy punktów."""
    z = Zestaw(nazwa)
    suma = sum(x["pkt"] or 0 for x in z)
    acc, c1 = 0, []
    for x in z:
        if acc + (x["pkt"] or 0) > suma / 2 and c1:
            break
        c1.append(x)
        acc += x["pkt"] or 0
    c2 = z[len(c1):]
    return {"zestaw": nazwa, "suma": suma,
            "czesc_1": {"zadania": [x["nr"] for x in c1], "ids": [x["id"] for x in c1], "pkt": acc},
            "czesc_2": {"zadania": [x["nr"] for x in c2], "ids": [x["id"] for x in c2], "pkt": suma - acc}}


def Dzial(id: int) -> str:
    """Dział (makro_dzial segmentu A) o największej wadze w wektorze zadania."""
    v = _e.vectors[_e.id_to_idx[int(id)]]
    s = Counter()
    for d in _e.dims_list:
        if d.get("segment") == "A" and v[d["id"]] > 0:
            s[d["makro_dzial"]] += float(v[d["id"]])
    return s.most_common(1)[0][0] if s else "?"


def _ostatnie_proby() -> list[tuple[int, str, str, float]]:
    with _con() as c:
        return c.execute("""SELECT h.id, h.result, h.note, h.timestamp FROM history h
                            JOIN (SELECT id, MAX(entry_id) e FROM history GROUP BY id) x ON h.entry_id = x.e""").fetchall()


def Pokrycie() -> list[dict]:
    """Dla każdego działu: liczba prób, średni wynik 0–10 (ostatnia próba każdego zadania), liczba fail."""
    agg: dict[str, list] = defaultdict(list)
    for tid, res, note, _ in _ostatnie_proby():
        agg[Dzial(tid)].append(_extract_task_score(res, note or ""))
    dzialy = sorted({d["makro_dzial"] for d in _e.dims_list if d.get("segment") == "A"})
    return sorted(({"dzial": d, "proby": len(agg[d]),
                    "sredni_wynik": round(float(np.mean(agg[d])), 1) if agg[d] else None,
                    "fail": sum(1 for s in agg[d] if s <= 1)} for d in dzialy),
                  key=lambda x: (x["sredni_wynik"] is not None, x["sredni_wynik"] or 0))


def Pewniaki(min_prob: int = MIN_PROB_PEWNIAKA) -> dict:
    """Typy zadań z każdej matury 2023–2026 (PEWNIAKI) i ile zadań każdego typu jest już w historii.

    Zwraca lista (nazwa, pkt, proby, sredni_wynik 0–10, ok = proby >= min_prob), braki i tekst do raportu."""
    seg_a = [d["id"] for d in _e.dims_list if d.get("segment") == "A"]
    ost = _ostatnie_proby()
    lista = []
    for nazwa, klucze, pkt in PEWNIAKI:
        q = np.zeros(len(_e.dims_list), dtype=bool)
        for k in klucze:
            q |= np.array(Wektor({k: 1}), dtype=np.float32) > 0
        wyn = []
        for tid, res, note, _ in ost:
            if tid not in _e.id_to_idx:
                continue
            v = np.maximum(_e.vectors[_e.id_to_idx[tid]], 0)
            a = float(v[seg_a].sum())
            if a > 0 and float(v[q].sum()) / a >= 0.3:
                wyn.append(_extract_task_score(res, note or ""))
        lista.append({"nazwa": nazwa, "pkt": pkt, "proby": len(wyn),
                      "sredni_wynik": round(float(np.mean(wyn)), 1) if wyn else None, "ok": len(wyn) >= min_prob})
    braki = [x for x in lista if not x["ok"]]
    tekst = (f"Pewniaki matury (typy z każdej matury 2023–2026) z co najmniej {min_prob} próbami: {len(lista) - len(braki)}/{len(lista)}"
             + ("; za mało: " + ", ".join(f"{x['nazwa']} ({x['proby']})" for x in braki) if braki else "") + ".")
    return {"lista": lista, "braki": [x["nazwa"] for x in braki], "tekst": tekst}


TYPY = ("rachunkowy", "pojęciowy", "metoda", "warunki", "niedokończone", "odczyt", "zapis")


def Zapisz(id: int, pkt: float, maks: int, typ: str = "", opis: str = "", powt: int | None = None, pomoc: bool = False) -> str:
    """Odnotuj z wynikiem z punktów. ok = komplet bez pomocy, fail = 0 pkt lub zła metoda, reszta partial.

    Notatka: '[pkt: x/10] [typ] opis; pkt/maks pkt; powt:<id>'. Zwraca zapisaną notatkę.
    """
    wynik, note = _notatka_zapisu(pkt, maks, typ, opis, powt, pomoc)
    for proba in range(3):
        try:
            Odnotuj(int(id), wynik, note=note)
            break
        except sqlite3.OperationalError:
            if proba == 2:
                raise
            time.sleep(3)
    return note


def _notatka_zapisu(pkt: float, maks: int, typ: str, opis: str, powt: int | None, pomoc: bool) -> tuple[str, str]:
    """(wynik, notatka) jak w Zapisz; wspólne z Popraw."""
    if typ and typ not in TYPY:
        raise ValueError(f"typ musi być jednym z {TYPY}")
    if not 0 <= pkt <= maks or maks <= 0:
        raise ValueError(f"punkty poza zakresem: {pkt}/{maks}")
    wynik = "ok" if pkt >= maks and not pomoc else ("fail" if pkt == 0 else "partial")
    # Ocena 0–10 w notatce czyta Opanowanie; wynik podajemy wprost, bo score= w Odnotuj
    # zamieniłby np. 3/5 pkt na 'ok'.
    note = f"[pkt: {round(10 * pkt / maks, 1):g}/10] " + (f"[{typ}] " if typ else "")
    note += (opis or ("czysto" if wynik == "ok" else "")) + f"; {pkt:g}/{maks} pkt"
    if pomoc:
        note += "; z pomocą"
    if powt:
        note += f"; powt:{int(powt)}"
    return wynik, note


def Popraw(id: int, pkt: float, maks: int, typ: str = "", opis: str = "", powt: int | None = None, pomoc: bool = False) -> str:
    """Poprawia OSTATNI zapis zadania (pomyłka w ocenie, uczeń dosłał brakującą część), zamiast drugiego
    Zapisz, które liczyłoby się jako nowa próba. Data próby zostaje, więc Wynik nadal przypisze ją do
    tego samego arkusza. Argumenty jak w Zapisz (pełna nowa notatka). Zwraca zapisaną notatkę."""
    tid = int(id)
    wynik, note = _notatka_zapisu(pkt, maks, typ, opis, powt, pomoc)
    conn = _engine.get_conn()
    try:
        with conn:
            cur = conn.cursor()
            row = cur.execute("SELECT entry_id, attempt, timestamp FROM history WHERE id = ? ORDER BY entry_id DESC LIMIT 1",
                              (tid,)).fetchone()
            if not row:
                raise MaturaError(f"Zadanie {tid} nie ma jeszcze zapisu: użyj Zapisz.")
            entry, attempt, ts = row
            cur.execute("UPDATE history SET result = ?, note = ? WHERE entry_id = ?", (wynik, note, entry))
            if wynik in ("ok", "partial"):
                cur.execute("""INSERT INTO done (id, result, note, attempt, timestamp) VALUES (?, ?, ?, ?, ?)
                               ON CONFLICT(id) DO UPDATE SET result = excluded.result, note = excluded.note,
                               attempt = excluded.attempt, timestamp = excluded.timestamp""",
                            (tid, wynik, note, attempt, ts))
                cur.execute("DELETE FROM cooldown_fails WHERE id = ?", (tid,))
                _engine.done_set.add(tid)
            else:
                cur.execute("DELETE FROM done WHERE id = ?", (tid,))
                cur.execute("""INSERT INTO cooldown_fails (id, failed_at) VALUES (?, ?)
                               ON CONFLICT(id) DO UPDATE SET failed_at = excluded.failed_at""", (tid, ts))
                _engine.done_set.discard(tid)
    finally:
        conn.close()
    return note


_PKT_NOTATKA = re.compile(r";\s*([0-9.]+)/(\d+) pkt")


def Wynik(folder: str | Path, zapisz: bool = True) -> dict:
    """Wynik arkusza z prób zapisanych po jego utworzeniu; zapisuje WYNIK.md (tabela, suma, procent,
    najczęstsze typy błędów). Zadania bez wpisu są wypisane jako brakujące.

    Kolumna „Kraj” i sekcja „Na tle kraju” (m.Latwosc): ile punktów zdobyłby w tych zadaniach
    przeciętny maturzysta, błędy w zadaniach łatwych dla kraju (>= 60%) do omówienia najpierw,
    a dla matury majowej z rezerwy (ZestawyCKE) wynik wobec średniej kraju. Bez łatwości sekcji nie ma."""
    folder = Path(folder) if Path(folder).is_absolute() else ARKUSZE / folder
    z = json.loads((folder / "zadania.json").read_text(encoding="utf-8"))
    od = _start_arkusza(folder)
    wiersze, suma, maks_suma, typy, brak = [], 0.0, 0, Counter(), []
    kraj = {"oczek": 0.0, "pkt": 0.0, "maks": 0, "zadania": 0, "szacunek": 0, "latwe_bledy": []}
    with _con() as c:
        for t in z.get("zadania", []):
            r = c.execute("SELECT result, note FROM history WHERE id = ? AND timestamp >= ? ORDER BY entry_id DESC LIMIT 1",
                          (t["id"], od)).fetchone()
            if not r:
                brak.append(t["nr"])
                wiersze.append(f"| {t['nr']} | {t['id']} | – | {LatwoscTekst(t['id'])} | brak wpisu |")
                continue
            p = _PKT_NOTATKA.search(r[1] or "")
            typ = (re.search(r"\[(" + "|".join(TYPY) + r")\]", r[1] or "") or [None, ""])[1]
            if p:
                suma += float(p.group(1))
                maks_suma += int(p.group(2))
                pk = f"{float(p.group(1)):g}/{p.group(2)}"
            else:
                pk = r[0]
            if typ:
                typy[typ] += 1
            l = Latwosc(t["id"])
            if l and p:
                kraj["oczek"] += int(p.group(2)) * l["latwosc"] / 100
                kraj["pkt"] += float(p.group(1))
                kraj["maks"] += int(p.group(2))
                kraj["zadania"] += 1
                kraj["szacunek"] += l["zrodlo"] == "szacunek"
                if l["latwosc"] >= 60 and float(p.group(1)) < int(p.group(2)):
                    kraj["latwe_bledy"].append(t["nr"])
            wiersze.append(f"| {t['nr']} | {t['id']} | {pk} | {LatwoscTekst(t['id'])} | {typ or ('—' if r[0] == 'ok' else r[0])} |")
    proc = round(100 * suma / maks_suma) if maks_suma else None
    md = [f"# Wynik: {z.get('tytul', folder.name)}", "", "| Nr | id | Punkty | Kraj | Typ błędu |", "| --- | --- | --- | --- | --- |", *wiersze, "",
          f"**Suma: {suma:g}/{maks_suma} pkt" + (f" ({proc}%)**" if proc is not None else "**")]
    if typy:
        md.append("Najczęstsze typy błędów: " + ", ".join(f"{k} ×{v}" for k, v in typy.most_common(3)) + ".")
    if brak:
        md.append(f"Bez wpisu: zadania {', '.join(map(str, brak))}.")
    ids = [int(t["id"]) for t in z.get("zadania", [])]
    matura = next(((n, x) for n, x in ZestawyCKE().items() if set(ids) and set(ids) <= set(x["ids"])), None)
    if kraj["zadania"] or matura:
        md += ["", "## Na tle kraju", ""]
    if kraj["zadania"]:
        prz = "≈" if kraj["szacunek"] else ""
        md.append(f"W {kraj['zadania']} zadaniach z łatwością: {kraj['pkt']:g}/{kraj['maks']} pkt, przeciętny maturzysta "
                  f"zdobyłby {prz}{kraj['oczek']:.1f} pkt"
                  + (f" (łatwość CKE dla {kraj['zadania'] - kraj['szacunek']}, szacunek dla {kraj['szacunek']})." if kraj["szacunek"] else " (dane CKE)."))
        if kraj["latwe_bledy"]:
            md.append(f"Najpierw omów zadania {', '.join(map(str, kraj['latwe_bledy']))}: kraj rozwiązuje je w co najmniej 60%.")
    if matura and proc is not None and not brak and maks_suma == 50:
        n, x = matura
        zd = f"{x['zdajacych']:,}".replace(",", " ")
        md.append(f"Matura {n[4:8]} ({n}): {proc}% wobec średniej kraju {x['srednia']}% "
                  f"(mediana {x['mediana']}%, {zd} zdających).")
    # Wagi CKE: pełny arkusz 50 pkt -> centyl krajowy; zawsze prognoza matury ze wszystkich prób.
    c = Centyl(proc, int(matura[0][4:8]) if matura and int(matura[0][4:8]) in ROZKLAD_KRAJU else None) \
        if proc is not None and not brak and maks_suma == 50 else None
    pg = Prognoza()
    if c is not None or pg["status"] == "ok":
        if "## Na tle kraju" not in md:
            md += ["", "## Na tle kraju", ""]
        if c is not None:
            md.append(f"{proc}% na maturze to wynik lepszy niż ok. {c}% zdających rozszerzoną (rozkład z lat {', '.join(map(str, ROZKLAD_KRAJU))}).")
        if pg["status"] == "ok":
            md.append(pg["tekst"])
    if zapisz:
        (folder / "WYNIK.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return {"suma": suma, "maks": maks_suma, "procent": proc, "typy": dict(typy), "brak": brak, "kraj": kraj,
            "matura": matura[0] if matura else None, "plik": str(folder / "WYNIK.md")}


def _pkt_proby(tid: int, result: str, note: str) -> tuple[float, int] | None:
    """(punkty, maks) próby: z notatki „; x/maks pkt”, inaczej z oceny 0–10 i punktów zadania."""
    p = _PKT_NOTATKA.search(note or "")
    if p:
        return float(p.group(1)), int(p.group(2))
    maks = Punkty_plan(tid)
    return (_extract_task_score(result, note or "") / 10 * maks, maks) if maks else None


def NaTleKraju(dni: int | None = None) -> dict:
    """Wyniki ucznia wobec kraju (m.Latwosc), ostatnia próba każdego zadania (dni = tylko ostatnie dni).

    Zwraca dzialy (pkt, oczekiwane pkt przeciętnego maturzysty, liczba zadań), trudne (zadania
    o łatwości < 30%: pkt, maks, ile zadań), szacunek (ile ocen z szacunku) i tekst do raportu.
    Zadania bez łatwości są pomijane; bez żadnego zadania z łatwością tekst = ''."""
    od = time.time() - dni * 86400 if dni else 0
    with _con() as c:
        rows = c.execute("SELECT id, result, note FROM history WHERE timestamp >= ? ORDER BY entry_id", (od,)).fetchall()
    ost = {int(i): (r, n) for i, r, n in rows}
    dz: dict[str, list[float]] = {}
    trudne = [0.0, 0, 0]
    szac = 0
    for tid, (r, n) in ost.items():
        l, pk = Latwosc(tid), _pkt_proby(tid, r, n)
        if not l or not pk:
            continue
        szac += l["zrodlo"] == "szacunek"
        a = dz.setdefault(Dzial(tid), [0.0, 0.0, 0, 0])
        a[0] += pk[0]
        a[1] += pk[1] * l["latwosc"] / 100
        a[2] += 1
        a[3] += pk[1]
        if l["latwosc"] < 30:
            trudne[0] += pk[0]
            trudne[1] += pk[1]
            trudne[2] += 1
    dzialy = sorted(({"dzial": k, "pkt": round(v[0], 1), "oczek": round(v[1], 1), "maks": v[3], "zadania": v[2]}
                     for k, v in dz.items()), key=lambda x: x["pkt"] - x["oczek"])
    n_all = sum(x["zadania"] for x in dzialy)
    tekst = ""
    if n_all:
        pkt, oczek, maks = sum(x["pkt"] for x in dzialy), sum(x["oczek"] for x in dzialy), sum(x["maks"] for x in dzialy)
        tekst = (f"Na tle kraju ({n_all} zadań, szacunek łatwości dla {szac}): {pkt:g}/{maks} pkt, przeciętny maturzysta "
                 f"{'≈' if szac else ''}{oczek:.1f} pkt")
        if trudne[2]:
            tekst += f"; zadania trudne dla kraju (< 30%): {trudne[0]:g}/{trudne[1]} pkt ({100 * trudne[0] / trudne[1]:.0f}%, {trudne[2]} zad.)"
        tekst += ". Działy: " + ", ".join(f"{x['dzial']} {x['pkt']:g} vs {x['oczek']:g}" for x in dzialy[:6])
    return {"dzialy": dzialy, "trudne": {"pkt": trudne[0], "maks": trudne[1], "zadania": trudne[2]},
            "szacunek": szac, "tekst": tekst}


# ---------------------------------------------------------------- analiza i propozycja arkusza

def Raport(dni: int = 14) -> str:
    """Wszystkie dane do sekcji „Analiza postępów” w jednym zwięzłym tekście (nic nie zapisuje)."""
    st = Statystyki()
    t = Tydzien()
    lin = [f"Dziś {Dzis():%d.%m.%Y}, tydzień {t['tydzien']} ({t['faza']}): {t['blok']}; length {t['length']}"
           + (f"; pełny arkusz {t['pelny']}" if t.get("pelny") else "")]
    lin.append(f"Próby: {st['laczna_liczba_prob']}, zadania zrobione: {st['zrobione']} (samodzielnie {st['sukces_samodzielny']}, z błędem/pomocą {st['zrobione_z_pomoca']})")
    od = time.time() - dni * 86400
    with _con() as c:
        ost = c.execute("SELECT id, result, note FROM history WHERE timestamp >= ?", (od,)).fetchall()
    if ost:
        sc = [_extract_task_score(r, n or "") for _, r, n in ost]
        lin.append(f"Ostatnie {dni} dni: {len(ost)} prób, średnio {np.mean(sc):.1f}/10, fail {sum(r == 'fail' for _, r, _ in ost)}")
        ty = Counter(x for _, _, n in ost for x in re.findall(r"\[(" + "|".join(TYPY) + r")\]", n or ""))
        if ty:
            lin.append("Typy błędów: " + ", ".join(f"{k} ×{v}" for k, v in ty.most_common()))
    # Mocne i słabe strony: m.Profil (trudność zadań, świeżość prób, przedziały niepewności).
    # W symulacji trafniej niż średnie z Pokrycie i Slabe (README, „Profil umiejętności”).
    pr = Profil()
    if pr["status"] == "ok":
        lin.append(pr["raport"])
    ntk = NaTleKraju()
    if ntk["tekst"]:
        lin.append(ntk["tekst"])
    pg = Prognoza()
    if pg["status"] == "ok":
        lin.append(pg["tekst"])
    lin.append(Pewniaki()["tekst"])
    pw = Powtorki()
    lin.append(f"Zaległe powtórki: {len(pw)}" + (" (" + ", ".join(f"{x['pierwotne']} +{x['zalegle_dni']} dni" for x in pw[:6]) + ")" if pw else ""))
    ns = Niesprawdzone()
    if ns:
        lin.append("Niesprawdzone arkusze: " + ", ".join(f"{k} ({len(v)} zad.)" for k, v in ns.items()))
    analizy = sorted(ARKUSZE.glob("*/ANALIZA.md"))
    if analizy:
        lin.append(f"Poprzednia analiza: {analizy[-1]}")
    return "\n".join(lin)


def _klucz(spec) -> str:
    return next(iter(spec)) if isinstance(spec, dict) else str(spec)


def _przypisz_klucz(tid: int, klucze: list[dict]) -> str | None:
    """Klucz harmonogramu najbliższy zadaniu (cosinus), gdy zadania.json nie ma pola 'klucz'."""
    if tid not in _e.id_to_idx or not klucze:
        return None
    v = _e.vectors[_e.id_to_idx[tid]]
    best, kb = -1.0, None
    for k in klucze:
        q = np.array(Wektor(k), dtype=np.float32)
        s = float(v @ q / (np.linalg.norm(v) * np.linalg.norm(q) + 1e-9))
        if s > best:
            best, kb = s, _klucz(k)
    return kb


def Propozycja(dzien: date | str | None = None, godzina: str | None = None, folder: str | Path | None = None) -> dict:
    """Plan arkusza według harmonogramu i zasad z matura-system: sloty z rolą, kluczem, length
    i 4 kandydatami (pierwszy to wybór domyślny; po obejrzeniu obrazu zły → następny kandydat i p.Odrzuc).

    Zwraca dict z polami: typ ('codzienny' | 'pelny' | 'pelny_czesc_2'), tydzien, faza, blok, decyzje
    (lista zdań do ANALIZA.md), sloty, tabela (markdown do ANALIZA.md). Gdy podano folder, zapisuje tam
    plan_arkusza.json, który matura.py arkusz dołącza do zadania.json (rola, klucz, powtórka).
    """
    h = Harmonogram()
    d = _dzien(dzien)
    t = Tydzien(d)
    dzien_tyg = DNI[d.weekday()]
    godziny = h["lekcje"].get(dzien_tyg, [])
    godzina = godzina or (godziny[0] if godziny else None)
    decyzje: list[str] = []
    wynik = {"data": d.isoformat(), "godzina": godzina, "tydzien": t["tydzien"], "faza": t["faza"], "blok": t["blok"]}
    if godzina and godziny and godzina not in godziny:
        decyzje.append(f"Uwaga: w harmonogramie {dzien_tyg} ma lekcje {', '.join(godziny)}, nie {godzina}.")

    # Pełny arkusz próbny
    pa = h.get("pelny_arkusz", {})
    if t.get("pelny") and dzien_tyg == pa.get("dzien", "wt"):
        pd = Podzial(t["pelny"])
        druga = bool(godziny) and godzina == godziny[-1] and len(godziny) > 1
        wynik.update({"typ": "pelny_czesc_2" if druga else "pelny", "zestaw": t["pelny"], "podzial": pd,
                      "ids": [x["id"] for x in Zestaw(t["pelny"])], "czas": pa.get("limit_minut", 180)})
        wynik["decyzje"] = decyzje + [
            f"Pełny arkusz {t['pelny']} ({pd['suma']} pkt): część 1 to zadania {pd['czesc_1']['zadania'][0]}–{pd['czesc_1']['zadania'][-1]} ({pd['czesc_1']['pkt']} pkt), "
            f"część 2 to {pd['czesc_2']['zadania'][0]}–{pd['czesc_2']['zadania'][-1]} ({pd['czesc_2']['pkt']} pkt)."]
        cke = ZestawyCKE().get(t["pelny"])
        if cke:
            wynik["decyzje"].append(f"Prawdziwa matura ({t['pelny']}, średnia kraju {cke['srednia']}%): obrazy to całe strony, "
                                    f"więc matura.py arkusz z opcjami {cke.get('arkusz_py', '')}; łatwości nie drukować.")
        return wynik

    sklad = dict(t["sklad"])
    klucze = t["klucze"] or []
    lo, hi = t["length"]
    teraz = f"{d.isoformat()}_{(godzina or '0000').replace(':', '')}"

    # Arkusze tego tygodnia i tego bloku przed bieżącą lekcją
    poprzednie = [(n, z) for n, z in _arkusze() if n < teraz]
    tydz = [(n, z) for n, z in poprzednie if t["od"] <= n[:10] <= t["do"]]
    uzycie = Counter()
    for _, z in tydz:
        for x in z.get("zadania", []):
            if x.get("rola") in (None, "blok", "diagnoza"):
                k = x.get("klucz") or _przypisz_klucz(x["id"], klucze)
                if k:
                    uzycie[k] += 1
    bloki = [w for w in h["tygodnie"] if w["blok"] == t["blok"]]
    w_bloku = sum(1 for n, _ in poprzednie if any(w["od"] <= n[:10] <= w["do"] for w in bloki))

    # Trudność z ostatnich prób
    with _con() as c:
        ost = c.execute("SELECT result, note FROM history ORDER BY entry_id DESC LIMIT 12").fetchall()
    sc = [_extract_task_score(r, n or "") for r, n in ost]
    trzy_fail = len(ost) >= 3 and all(r == "fail" for r, _ in ost[:3])
    if trzy_fail:
        L, powod = lo + 0.25 * (hi - lo), "trzy ostatnie próby to fail, więc dolna część zakresu"
    elif w_bloku < 2 and t["faza"][0] in "12":
        L, powod = lo + 0.25 * (hi - lo), f"lekcja {w_bloku + 1} w nowym bloku, więc dolna część zakresu"
    elif len(sc) >= 6 and np.mean(sc) > 8.5:
        L, powod = lo + 0.8 * (hi - lo), f"średnio {np.mean(sc):.1f}/10 w ostatnich próbach, więc górna część zakresu"
    else:
        L, powod = (lo + hi) / 2, "środek zakresu"
    L = round(L, 2)
    decyzje.append(f"length {L} ({powod}).")

    sloty: list[dict] = []
    wybrane: set[int] = set()

    def dodaj(rola: str, spec, dl: float, powt: int | None = None, kandydaci: list[int] | None = None,
              lat: tuple | None = None):
        if isinstance(spec, str):
            spec = {spec: 3}
        k = kandydaci if kandydaci is not None else Szukaj(spec, length=dl, samples=4, exclude=wybrane, latwosc=lat)
        wybrane.update(k)
        if k:
            sloty.append({"rola": rola, "klucz": _klucz(spec), "length": round(dl, 2), "powt": powt, "kandydaci": k})
        else:
            decyzje.append(f"Brak kandydatów dla {rola} „{_klucz(spec)}”.")

    # Powtórki
    pw = Powtorki(d)
    n_pow = sklad.get("powtorki", 0)
    if n_pow and len(pw) > 4:
        n_pow = 3
        decyzje.append(f"{len(pw)} zaległych powtórek, więc 3 w arkuszu.")
    pw_uzyte = pw[:n_pow]
    if n_pow and len(pw_uzyte) < n_pow:
        decyzje.append(f"Powtórek do zrobienia: {len(pw_uzyte)}, brakujące miejsca idą do bloku.")
        sklad["blok"] = sklad.get("blok", 0) + n_pow - len(pw_uzyte)

    # Luki po bloku: dział słaby według Profilu po swoim bloku dostaje +1 zadanie (najwyżej 6 zadań)
    pokrycie = Pokrycie()
    profil = Profil()
    opan = {x["dzial"]: x for x in profil["dzialy"]}
    przerobione = {Dzial_klucza(k) for w in h["tygodnie"] if w["do"] < d.isoformat() and w["faza"][0] in "12" for k in w["klucze"]}
    # Wagi CKE: dział słaby po bloku dostaje +1 zadanie tylko, gdy jest wart co najmniej WAGA_PLUS1 pkt
    # na maturze; pierwszy jest ten, w którym najwięcej punktów ucieka (_strata).
    slabe = sorted((x for x in profil["slabe"] if x["dzial"] in przerobione and WagaMatury(x["dzial"]) >= WAGA_PLUS1),
                   key=_strata, reverse=True)

    faza = t["faza"][0]
    if faza == "0":
        # Diagnoza: po jednym zadaniu z kluczy z najmniejszą liczbą użyć w tym tygodniu i prób w historii
        proby_klucza = Counter()
        for tid, *_ in _ostatnie_proby():
            k = _przypisz_klucz(tid, klucze)
            if k:
                proby_klucza[k] += 1
        kolej = sorted(klucze, key=lambda k: (uzycie[_klucz(k)] + proby_klucza[_klucz(k)], klucze.index(k)))
        for k in kolej[: sklad.get("blok", 6)]:
            dodaj("diagnoza", k, L)
        decyzje.append("Diagnoza: klucze z najmniejszą liczbą zadań w tym tygodniu: " + ", ".join(_klucz(k) for k in kolej[: sklad.get('blok', 6)]) + ".")
    else:
        for p_ in pw_uzyte:
            kand = Blizniak(p_["pierwotne"], samples=4, exclude=wybrane)
            dodaj("powtorka", Dzial(p_["pierwotne"]), L, powt=p_["pierwotne"], kandydaci=kand)
        if faza in "34":
            # Najpierw działy słabe według Profilu, potem te z największą niepewnością
            # (jedno zadanie tam powie najwięcej), na końcu działy bez żadnej próby. W każdej grupie
            # kolejność według punktów uciekających na maturze (_strata, wagi CKE): w symulacji
            # +3 do +4 pkt proc. wyniku matury na 40 luk wobec samego opanowania.
            n_luk = sklad.get("luki", 0)
            luki = [x["dzial"] for x in sorted(profil["slabe"], key=_strata, reverse=True)][:n_luk]
            luki += [x["dzial"] for x in sorted(profil["do_sprawdzenia"], key=_strata, reverse=True)
                     if x["dzial"] not in luki][: n_luk - len(luki)]
            luki += [x["dzial"] for x in sorted(pokrycie, key=lambda x: -WagaMatury(x["dzial"]))
                     if x["sredni_wynik"] is None and x["dzial"] not in luki and WagaMatury(x["dzial"]) >= 1][: n_luk - len(luki)]
            for dz in luki:
                dodaj("luka", dz, round(lo + 0.75 * (hi - lo), 2))
            if luki:
                decyzje.append("Luki z profilu (słabe, potem niepewne, potem bez prób; w grupie najpierw najwięcej traconych punktów matury): " + ", ".join(
                    f"{x} {opan[x]['opanowanie']}%, ≈{_strata(opan[x]):.1f} pkt" if x in opan else f"{x} (brak prób, {WagaMatury(x):.1f} pkt na arkuszu)"
                    for x in luki) + ".")
        kolej = sorted(klucze, key=lambda k: (uzycie[_klucz(k)], klucze.index(k)))
        # Łatwość krajowa (m.Latwosc) w bloku: po trzech fail zadania, które kraj rozwiązuje w ponad połowie,
        # w fazie 2 zadania trudne dla kraju (do 30%). Zadania bez łatwości przechodzą jak dawniej.
        lat_bloku = (50, 100) if trzy_fail else ((0, 30) if faza == "2" else None)
        if lat_bloku and sklad.get("blok", 0) and kolej:
            decyzje.append("Blok: najpierw zadania o łatwości krajowej " + ("powyżej 50% (po trzech fail)." if trzy_fail else "do 30% (faza 2)."))
        for i in range(sklad.get("blok", 0)):
            if kolej:
                dodaj("blok", kolej[i % len(kolej)], L, lat=lat_bloku)
        if slabe and len(sloty) < 6:
            dodaj("luka", slabe[0]["dzial"], round((lo + hi) / 2, 2))
            decyzje.append(f"{slabe[0]['dzial']}: opanowanie {slabe[0]['opanowanie']}% (przedział {slabe[0]['przedzial'][0]}–{slabe[0]['przedzial'][1]}) po swoim bloku, "
                           f"na maturze {WagaMatury(slabe[0]['dzial']):.1f} pkt, więc +1 zadanie.")
        wczesniejsze = [k for w in h["tygodnie"] if w["od"] < t["od"] and w["faza"][0] in "012" for k in w["klucze"]]
        wczesniejsze = [k for k in wczesniejsze if _klucz(k) not in {_klucz(x) for x in klucze}] or wczesniejsze
        wczesniejsze = list({_klucz(k): k for k in wczesniejsze}.values())
        if wczesniejsze:
            rng = np.random.default_rng(d.toordinal() * 100 + int((godzina or "0").replace(":", "")[:2] or 0))
            # Mieszane: najpierw wcześniejsze działy, w których najwięcej punktów matury ucieka
            # (waga CKE x (1 - opanowanie z Profilu), brak prób = 50%), z losowym szumem,
            # żeby nie wracał wciąż ten sam klucz.
            def _waga(k):
                dz = Dzial_klucza(k)
                x = opan.get(dz)
                return -WagaMatury(dz) * (1 - (x["opanowanie"] if x else 50) / 100) * np.exp(rng.normal(0, 0.3))
            idx = sorted(range(len(wczesniejsze)), key=lambda j: _waga(wczesniejsze[j]))
            n_mies = sklad.get("mieszane", 0)
            # Puste miejsca (brak powtórek, brak kluczy bloku) dopełniają mieszane do minimum zadań lekcji.
            n_mies += max(0, min(h["lekcja"]["zadania"]) - len(sloty) - n_mies)
            for j in range(n_mies):
                dodaj("mieszane", wczesniejsze[idx[j % len(idx)]], round((lo + hi) / 2, 2))
    wynik.update({"typ": "codzienny", "decyzje": decyzje, "sloty": sloty})
    wynik["tabela"] = "\n".join(
        ["| Nr | Rola | Klucz | id | length | Zapas |", "| --- | --- | --- | --- | --- | --- |"]
        + [f"| {i} | {s['rola']}{' ← ' + str(s['powt']) if s['powt'] else ''} | {s['klucz']} | {s['kandydaci'][0]} | {s['length']} | {', '.join(map(str, s['kandydaci'][1:]))} |"
           for i, s in enumerate(sloty, 1)])
    if folder:
        f = Path(folder) if Path(folder).is_absolute() else ARKUSZE / folder
        f.mkdir(parents=True, exist_ok=True)
        (f / "plan_arkusza.json").write_text(json.dumps(wynik, ensure_ascii=False, indent=1), encoding="utf-8")
    return wynik


def Dzial_klucza(spec) -> str:
    """Dział (makro_dzial) klucza harmonogramu, np. {'Granica ciągu': 3} -> 'Ciągi liczbowe'."""
    v = np.array(Wektor(spec if isinstance(spec, dict) else {spec: 1}), dtype=np.float32)
    s = Counter()
    for d in _e.dims_list:
        if d.get("segment") == "A" and v[d["id"]] > 0:
            s[d["makro_dzial"]] += float(v[d["id"]])
    return s.most_common(1)[0][0] if s else "?"


# ---------------------------------------------------------------- elastyczność planu
# Harmonogram to kolejka bloków, a nie sztywny kalendarz. Daty, przerwa świąteczna i pełne
# arkusze zostają na swoich tygodniach; przesuwa się tylko treść tygodni (faza, blok, klucze, length).
# Faza „3 Arkusze” jest buforem: wydłużenie bloku zabiera jej tydzień, skrócenie oddaje.

_TRESC = ("faza", "blok", "klucze", "length", "uwagi")
_STALE = "Przerwa świąteczna"
MIN_TYGODNI_ARKUSZY = 4


def _zapisz_harmonogram(h: dict, zmiana: str, powod: str) -> None:
    HARMONOGRAM.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")
    log = NOTATKI / "postep.md"
    s = log.read_text(encoding="utf-8")
    wiersz = f"| {Dzis():%d.%m.%Y} | {zmiana} | {powod} |"
    if "## Zmiany planu" in s and wiersz not in s:
        log.write_text(s.rstrip("\n") + "\n" + wiersz + "\n", encoding="utf-8")


def _ruchome(h: dict, od: int) -> list[int]:
    """Indeksy tygodni od numeru `od` (1-based), które można przesuwać (bez przerwy świątecznej)."""
    return [i for i, t in enumerate(h["tygodnie"]) if t["tydzien"] >= od and t["faza"] != _STALE]


def Wydluz(tydzien: int, powod: str) -> str:
    """Blok z tygodnia `tydzien` trwa tydzień dłużej; dalsze bloki przesuwają się o tydzień,
    faza arkuszy traci jeden tydzień (najwyżej do MIN_TYGODNI_ARKUSZY)."""
    h = Harmonogram()
    idx = _ruchome(h, tydzien)
    tresc = [{k: h["tygodnie"][i][k] for k in _TRESC} for i in idx]
    arkusze = [j for j, t in enumerate(tresc) if t["faza"].startswith("3")]
    if len(arkusze) <= MIN_TYGODNI_ARKUSZY:
        return f"Nie wydłużam: faza arkuszy ma już tylko {len(arkusze)} tygodnie."
    tresc.insert(1, dict(tresc[0]))
    del tresc[arkusze[0] + 1]
    for i, t in zip(idx, tresc):
        h["tygodnie"][i].update(t)
    _zapisz_harmonogram(h, f"Wydłużony blok „{tresc[0]['blok']}” (tydz. {tydzien})", powod)
    return "ok"


def Skroc(tydzien: int, powod: str) -> str:
    """Blok z tygodnia `tydzien` jest opanowany: wypada, dalsze bloki cofają się o tydzień,
    a faza arkuszy zyskuje tydzień."""
    h = Harmonogram()
    idx = _ruchome(h, tydzien)
    tresc = [{k: h["tygodnie"][i][k] for k in _TRESC} for i in idx]
    if tresc[0]["faza"][0] not in "12":
        return "Skracać można tylko bloki z faz 1 i 2."
    usuniety = tresc.pop(0)
    pierwszy_arkusz = next(t for t in tresc if t["faza"].startswith("3"))
    tresc.insert(tresc.index(pierwszy_arkusz), dict(pierwszy_arkusz))
    for i, t in zip(idx, tresc):
        h["tygodnie"][i].update(t)
    _zapisz_harmonogram(h, f"Pominięty blok „{usuniety['blok']}” (tydz. {tydzien})", powod)
    return "ok"


def Przeglad(dni: int = 14) -> dict:
    """Dane do przeglądu planu: tempo względem harmonogramu z ostatnich `dni` dni."""
    od = time.time() - dni * 86400
    with _con() as c:
        rows = c.execute("SELECT id, result, note, timestamp FROM history WHERE timestamp >= ?", (od,)).fetchall()
    wyniki = defaultdict(list)
    for tid, res, note, _ in rows:
        wyniki[Dzial(tid)].append(_extract_task_score(res, note or ""))
    t = Tydzien()
    h = Harmonogram()
    dzis = Dzis()
    bloki = [w for w in h["tygodnie"] if date.fromisoformat(w["do"]) < dzis and w["faza"][0] in "12"][-2:]
    return {
        "tydzien": t["tydzien"], "faza": t["faza"], "blok": t["blok"],
        "proby": len(rows),
        "sredni_wynik": round(float(np.mean([s for v in wyniki.values() for s in v])), 1) if rows else None,
        "dzialy": {d: (len(v), round(float(np.mean(v)), 1)) for d, v in wyniki.items()},
        "ostatnie_bloki": [(w["tydzien"], w["blok"]) for w in bloki],
        "tygodnie_arkuszy": sum(1 for w in h["tygodnie"] if w["faza"].startswith("3")),
        "zalegle_powtorki": len(Powtorki()),
        "niesprawdzone": Niesprawdzone(),
        "profil": {x["dzial"]: (x["opanowanie"], x["przedzial"], x["ocena"], x["proby"]) for x in Profil()["dzialy"]},
        # Wagi CKE: prognoza wobec progu fazy, punkty matury tracone w dziale (blok do wydłużenia
        # to ten, w którym ucieka najwięcej), pewniaki bez prób.
        "prognoza": Prognoza().get("tekst", ""),
        "strata_pkt": {x["dzial"]: round(_strata(x), 1) for x in sorted(Profil()["dzialy"], key=_strata, reverse=True)},
        "pewniaki_braki": Pewniaki()["braki"],
    }


# ==============================================================================
# SEKCJA: petla (dawniej matura.py petla)
# ==============================================================================

#!/usr/bin/env python3
# Pętla błędu: fiszki reguł, pytania rozgrzewki, pola wypełniane przez ucznia i ich odczyt.
#
# Metoda redukcji błędów: trzy bramki (plan, pewność, kontrola), fiszki „Jeśli …, to …”, rozgrzewka.
# Fiszki leżą w notatki/karty/reguly.md (tabela, jedna reguła „Jeśli …, to …” na wiersz); ten moduł
# prowadzi ich stan powtórek. Bazy matura.db tylko czyta.
#
# Rozgrzewka (mechanizm w PROCEDURY.md#rozgrzewka, prośba ucznia 23.10.2026): co lekcję nowe pytania
# do fiszek, które przypadają na powtórkę. Sesja arkusza układa je w pytania.json w folderze
# arkusza (matura.py petla rozgrzewka podaje fiszki, formy do wyboru i historię), matura.py petla pytania
# pilnuje formatu i tego, żeby się nie powtarzały (dziennik notatki/karty/pytania.md), matura.py arkusz
# drukuje je bez odpowiedzi, a klucz trafia do zadania.json dla sprawdzania.
#
# Pola z arkusza trafiają do notatki próby jako tagi na początku opisu w p.Zapisz
# (matura.py raport i p.Wynik je pomijają):
#     [pew:3]          pewność 1–3
#     [min:12]         minuty na zadanie (z godzin startu: matura.py petla czasy)
#     [utk:droga]      gdzie utknął: start (brak pomysłu), droga (w połowie), rachunki, czas (czas / zmęczenie)
#     [plan:tak]       linijka planu Cel · Droga · Ramka wypełniona (tak/nie)
#     [kon:WPS]        zaznaczone kroki kontroli: W wzory z tablic, P podstawienie, S sens, O odpowiedź; [kon:-] żaden
# Pole „z pomocą” to p.Zapisz(..., pomoc=True). Dane całej lekcji (godziny, sen, zmęczenie,
# najtrudniejsze, pytanie) idą do kontekst.json w folderze arkusza (matura.py petla kontekst).
#
# Użycie:
#     python3 matura.py petla                              # fiszki do powtórki dziś
#     python3 matura.py petla lista                        # wszystkie fiszki ze stanem
#     python3 matura.py petla rozgrzewka 2026-10-26_1630   # fiszki na lekcję: formy do wyboru, ostatnie konteksty i pytania
#     python3 matura.py petla pytania 2026-10-26_1630      # kontrola pytania.json przed złożeniem arkusza
#     python3 matura.py petla ocen 2026-10-26_1630 1:tak 2:nie 3:tak 4:tak 5:tak   # odpowiedzi na pytania 1–5 rozgrzewki
#     python3 matura.py petla ocen 2026-10-26_1630 3:tak --popraw                  # poprawka oceny
#     python3 matura.py petla dodaj "dzielę przez wyrażenie z x" "wyłączam je przed nawias" \
#         --przyklad "id 1054 (20.10)" --typ warunki --dzial Trygonometria
#     python3 matura.py petla wroc 7                       # błąd z fiszki 7 wrócił: od jutra znowu w rozgrzewce
#     python3 matura.py petla czasy 1:16:35 2:16:52 3:17:20 --koniec 18:02          # minuty na zadania -> [min:N]
#     python3 matura.py petla kontekst 2026-10-26_1630 --start 16:32 --koniec 18:02 --sen 7 --zm-start 2 --zm-koniec 4 \
#         --najtrudniejsze "dowód z okręgiem" --pytanie "czemu nie wolno dzielić przez sin x?"
#     python3 matura.py petla bramki 2026-10-26_1630      # sekcja „Bramki i kontekst” w WYNIK.md (po p.Wynik)
#
# Z Pythona: import petla; petla.Wybierz(5, folder), petla.Ocen(folder, {1: True}), petla.Czasy({1: "16:35"}, "18:02"),
# petla.Kontekst(folder, sen=7, ...), petla.Bramki(folder).


PLIK = NOTATKI / "karty" / "reguly.md"
PYTANIA = NOTATKI / "karty" / "pytania.md"
# Odstęp do następnej powtórki według serii udanych odpowiedzi z rzędu (0 = zapomniana).
INTERWALY = [1, 3, 7, 21, 28]
TYPY_petla = ("rachunkowy", "pojęciowy", "metoda", "warunki", "niedokończone", "odczyt", "zapis", "kontrola")
KOLUMNY = ("Nr", "Jeśli …", "to …", "Przykład", "Typ", "Dział", "Seria", "Ostatnio", "Następna", "Pokazana")
# Formy pytań rozgrzewki (PROCEDURY.md#rozgrzewka): kod -> nazwa nad pytaniem na arkuszu.
FORMY = {
    "blad": "Znajdź błąd",
    "mini": "Mini-zadanie",
    "warunki": "Najpierw warunki",
    "plan": "Tylko plan",
    "pf": "Prawda czy fałsz",
    "wynik": "Sprawdź wynik",
    "sygnal": "Gdzie pułapka",
}
# Formy, w których dana reguła naprawdę musi zadziałać (według typu błędu fiszki).
FORMY_DLA_TYPU = {
    "warunki": ("blad", "mini", "warunki", "pf", "wynik"),
    "pojęciowy": ("blad", "pf", "mini", "wynik"),
    "rachunkowy": ("wynik", "blad", "mini"),
    "niedokończone": ("plan", "sygnal", "mini"),
    "kontrola": ("wynik", "blad", "pf"),
    "odczyt": ("sygnal", "mini", "blad"),
    "zapis": ("blad", "pf", "mini"),
    "metoda": ("plan", "sygnal", "blad"),
}
KOLUMNY_PYTAN = ("Data", "Arkusz", "Nr", "Fiszka", "Forma", "Kontekst", "Pytanie", "Klucz", "Wynik")
_POLA_PYTAN = ("data", "arkusz", "nr", "fiszka", "forma", "kontekst", "pytanie", "klucz", "wynik")
PODOBNE = 0.8  # podobieństwo treści (difflib) od którego pytanie liczy się jako powtórzone
MAKS_ZNAKOW = 260  # dłuższe pytanie nie mieści się w 4 linijkach komórki arkusza
NAGLOWEK_PYTAN = """# Dziennik pytań rozgrzewki

Każde pytanie, które trafiło na arkusz. Wiersze dopisuje `matura.py petla`, gdy arkusz powstaje (`matura.py arkusz` bez `--test`), wynik dopisuje `matura.py petla ocen`. Na tej podstawie `matura.py petla pytania` pilnuje, żeby pytania i formy się nie powtarzały. Mechanizm: `PROCEDURY.md#rozgrzewka`. Ręcznie nie zmieniaj; odpowiedzi stąd nie trafiają na arkusz."""
SEKCJA = "## Bramki i kontekst"
_STARE_SEKCJE = ("## Bramki i pewność",)  # nazwa z pierwszej wersji (23.10.2026)
_PKT = re.compile(r";\s*([0-9.]+)/(\d+) pkt")
_PEW = re.compile(r"\[pew:\s*([123])\]")
_MIN = re.compile(r"\[min:\s*(\d+)\]")
_UTK = re.compile(r"\[utk:\s*(start|droga|rachunki|czas)\]")
_PLAN = re.compile(r"\[plan:\s*(tak|nie)\]")
_KON = re.compile(r"\[kon:\s*([WPSO]*|-)\]")
UTKNIECIA = {"start": "na starcie", "droga": "w połowie (droga)", "rachunki": "w rachunkach", "czas": "czas / zmęczenie"}
MIN_NA_PKT_MATURA = 180 / 50  # 3,6 min na punkt


def Dzis_petla() -> date:
    return datetime.now(TZ).date()


def _folder(f: str | Path) -> Path:
    f = Path(f)
    return f if f.is_absolute() else ARKUSZE / f


def _dzien_petla(folder: Path | None) -> date:
    """Dzień lekcji z nazwy folderu RRRR-MM-DD_GGMM, a bez folderu dziś."""
    if folder is not None:
        try:
            return date.fromisoformat(folder.name[:10])
        except ValueError:
            pass
    return Dzis_petla()


def _data_petla(s: str) -> date | None:
    return date.fromisoformat(s) if s and s != "—" else None


def _pl(x: float) -> str:
    """Liczba z przecinkiem dziesiętnym, jedna cyfra po przecinku."""
    return f"{x:.1f}".replace(".", ",")


# ---------------------------------------------------------------- plik z fiszkami

def _komorki(linia: str) -> list[str]:
    return [k.strip().replace("\\|", "|") for k in re.split(r"(?<!\\)\|", linia.strip())[1:-1]]


def _wczytaj() -> tuple[list[str], list[dict], list[str]]:
    """(linie przed tabelą, fiszki, linie po tabeli)."""
    przed, fiszki, po = [], [], []
    stan = 0  # 0 przed tabelą, 1 w tabeli, 2 po tabeli
    for linia in PLIK.read_text(encoding="utf-8").splitlines():
        tabela = linia.lstrip().startswith("|")
        if stan == 0 and tabela:
            stan = 1
        elif stan == 1 and not tabela:
            stan = 2
        if stan == 0:
            przed.append(linia)
        elif stan == 2:
            po.append(linia)
        else:
            k = _komorki(linia)
            if not k or not k[0].isdigit():
                continue  # nagłówek i separator
            k += ["—"] * (len(KOLUMNY) - len(k))
            fiszki.append({"nr": int(k[0]), "jesli": k[1], "to": k[2], "przyklad": k[3], "typ": k[4], "dzial": k[5],
                           "seria": int(k[6] or 0), "ostatnio": _data_petla(k[7]), "nastepna": _data_petla(k[8]) or Dzis_petla(),
                           "pokazana": "" if k[9] in ("", "—") else k[9]})
    return przed, fiszki, po


def Fiszki() -> list[dict]:
    """Wszystkie fiszki z notatki/karty/reguly.md w kolejności numerów."""
    return sorted(_wczytaj()[1], key=lambda f: f["nr"])


def _zapisz(fiszki: list[dict]) -> None:
    przed, _, po = _wczytaj()  # świeży odczyt: plik dzielą wątki, zmieniamy tylko tabelę
    esc = lambda s: str(s).replace("|", "\\|")  # noqa: E731
    wiersze = ["| " + " | ".join(KOLUMNY) + " |", "| " + " | ".join("---" for _ in KOLUMNY) + " |"]
    for f in sorted(fiszki, key=lambda f: f["nr"]):
        wiersze.append("| " + " | ".join(esc(x) for x in (
            f["nr"], f["jesli"], f["to"], f["przyklad"] or "—", f["typ"] or "—", f["dzial"] or "—", f["seria"],
            f["ostatnio"].isoformat() if f["ostatnio"] else "—", f["nastepna"].isoformat(), f["pokazana"] or "—")) + " |")
    while przed and not przed[-1].strip():
        przed.pop()
    PLIK.write_text("\n".join(przed + [""] + wiersze + po) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- rozgrzewka: fiszki na lekcję

def Wybierz(n: int = 5, folder: str | Path | None = None) -> list[dict]:
    """n fiszek na rozgrzewkę arkusza w folderze (nazwa RRRR-MM-DD_GGMM albo ścieżka).

    Najpierw zaległe (Następna ≤ dzień lekcji): zapomniane (seria 0 po ocenie albo po powrocie
    błędu) przed resztą, dalej najstarszy termin, nigdy niepokazane i dawno pokazane; brakujące
    do n uzupełniają najbliższe terminy. Pomija fiszki pokazane
    tego samego dnia w innym arkuszu (dwie lekcje we wtorek i czwartek). Gdy folder ma już
    pytania (pytania.json, a po złożeniu zadania.json), zwraca ich fiszki.
    """
    fiszki = Fiszki()
    folder = _folder(folder) if folder else None
    if folder:
        po_nr = {f["nr"]: f for f in fiszki}
        for plik, klucz in (("pytania.json", "pytania"), ("zadania.json", "rozgrzewka")):
            try:
                dane = json.loads((folder / plik).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            stare = (dane.get(klucz) if isinstance(dane, dict) else dane) or []
            wynik = []
            for s in stare:
                try:
                    wynik.append(po_nr[int(s["fiszka"])])
                except (TypeError, KeyError, ValueError):
                    pass
            if wynik:
                return wynik[:n]
    dzien = _dzien_petla(folder)
    nazwa = folder.name if folder else ""
    wolne = [f for f in fiszki if not (f["pokazana"][:10] == dzien.isoformat() and f["pokazana"] != nazwa)]
    pilna = lambda f: f["seria"] == 0 and f["ostatnio"] is not None  # noqa: E731  (zapomniana albo błąd wrócił)
    zalegle = sorted((f for f in wolne if f["nastepna"] <= dzien),
                     key=lambda f: (not pilna(f), f["nastepna"], f["pokazana"], f["nr"]))
    reszta = sorted((f for f in wolne if f["nastepna"] > dzien), key=lambda f: (f["nastepna"], f["nr"]))
    return (zalegle + reszta)[:n]


# ---------------------------------------------------------------- rozgrzewka: dziennik i nowe pytania

def _dziennik() -> tuple[list[str], list[dict]]:
    """(linie przed tabelą, wiersze) z notatki/karty/pytania.md; komórki jako tekst, <br> to nowa linia."""
    if not PYTANIA.exists():
        return [], []
    przed, wiersze, tabela = [], [], False
    for linia in PYTANIA.read_text(encoding="utf-8").splitlines():
        if linia.lstrip().startswith("|"):
            tabela = True
            k = _komorki(linia)
            if len(k) >= 3 and k[2].isdigit():
                k += ["—"] * (len(_POLA_PYTAN) - len(k))
                wiersze.append({p: v.replace("<br>", "\n") for p, v in zip(_POLA_PYTAN, k)})
        elif not tabela:
            przed.append(linia)
    return przed, wiersze


def _zapisz_dziennik(wiersze: list[dict]) -> None:
    przed = _dziennik()[0]  # świeży odczyt nagłówka: plik dzielą wątki
    while przed and not przed[-1].strip():
        przed.pop()
    przed = przed or NAGLOWEK_PYTAN.splitlines()
    esc = lambda s: str(s).replace("|", "\\|").replace("\n", "<br>")  # noqa: E731
    tab = ["| " + " | ".join(KOLUMNY_PYTAN) + " |", "| " + " | ".join("---" for _ in KOLUMNY_PYTAN) + " |"]
    for w in sorted(wiersze, key=lambda w: (w["data"], w["arkusz"], int(w["nr"]))):
        tab.append("| " + " | ".join(esc(w.get(p) or "—") for p in _POLA_PYTAN) + " |")
    PYTANIA.write_text("\n".join(przed + [""] + tab) + "\n", encoding="utf-8")


def Historia(fiszka: int | None = None, bez_arkusza: str | None = None) -> list[dict]:
    """Pytania z dziennika notatki/karty/pytania.md, najstarsze pierwsze. fiszka: tylko jej pytania;
    bez_arkusza: bez wierszy tego folderu (przy ponownym składzie arkusza)."""
    out = []
    for w in _dziennik()[1]:
        w["nr"] = int(w["nr"])
        w["fiszka"] = int(w["fiszka"]) if w["fiszka"].isdigit() else 0
        if (fiszka is None or w["fiszka"] == fiszka) and w["arkusz"] != bez_arkusza:
            out.append(w)
    return sorted(out, key=lambda w: (w["data"], w["arkusz"], w["nr"]))


def _formy_do_wyboru(f: dict, hist: list[dict]) -> list[str]:
    """Formy pasujące do typu fiszki, bez dwóch ostatnio użytych przy niej (o ile coś zostaje)."""
    dozwolone = list(FORMY_DLA_TYPU.get(f["typ"], FORMY))
    ostatnie = [h["forma"] for h in hist[-2:]]
    return ([x for x in dozwolone if x not in ostatnie]
            or [x for x in dozwolone if not ostatnie or x != ostatnie[-1]] or dozwolone)


def DoPytan(n: int = 5, folder: str | Path | None = None) -> list[dict]:
    """Fiszki na rozgrzewkę lekcji z tym, czego trzeba do ułożenia nowych pytań (PROCEDURY.md#rozgrzewka):
    formy do wyboru i trzy ostatnie pytania tej fiszki (data, forma, kontekst, treść, wynik)."""
    folder = _folder(folder) if folder else None
    wynik = []
    for f in Wybierz(n, folder):
        hist = Historia(f["nr"], bez_arkusza=folder.name if folder else None)
        wynik.append({**f, "formy": _formy_do_wyboru(f, hist), "historia": hist[-3:]})
    return wynik


def Pytania(folder: str | Path) -> list[dict]:
    """Pytania rozgrzewki z pytania.json w folderze arkusza, po kontroli formatu (ValueError przy błędzie).
    Format: {"pytania": [{"fiszka": 2, "forma": "blad", "kontekst": "…", "pytanie": "…", "klucz": "… Zalicz, gdy …"}]}."""
    plik = _folder(folder) / "pytania.json"
    try:
        dane = json.loads(plik.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"brak {plik} (pytania układa sesja arkusza według PROCEDURY.md#rozgrzewka)") from None
    except json.JSONDecodeError as e:
        raise ValueError(f"{plik.name}: zły JSON ({e})") from None
    lista = dane.get("pytania") if isinstance(dane, dict) else dane
    if not isinstance(lista, list) or not lista:
        raise ValueError(f"{plik.name}: oczekiwana niepusta lista „pytania”")
    out = []
    for i, p in enumerate(lista, 1):
        if not isinstance(p, dict):
            raise ValueError(f"pytanie {i}: oczekiwany obiekt z polami fiszka, forma, kontekst, pytanie, klucz")
        try:
            nr = int(p.get("fiszka"))
        except (TypeError, ValueError):
            raise ValueError(f"pytanie {i}: „fiszka” to numer z notatki/karty/reguly.md") from None
        forma = str(p.get("forma") or "").strip()
        if forma not in FORMY:
            raise ValueError(f"pytanie {i}: forma „{forma}” spoza: {', '.join(FORMY)}")
        tekst, klucz = str(p.get("pytanie") or "").strip(), str(p.get("klucz") or "").strip()
        if not tekst or not klucz:
            raise ValueError(f"pytanie {i}: puste „pytanie” albo „klucz”")
        out.append({"fiszka": nr, "forma": forma, "kontekst": str(p.get("kontekst") or "").strip(),
                    "pytanie": tekst, "klucz": klucz})
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def _przyklady() -> list[str]:
    """Przykładowe pytania z PROCEDURY.md#rozgrzewka (kolumna „Przykład” tabeli form i wzór pytania.json): nie na arkusz."""
    try:
        tekst = (BASE / "PROCEDURY.md").read_text(encoding="utf-8")
    except OSError:
        return []
    out = [k[-1] for k in map(_komorki, tekst.splitlines()) if len(k) == 5 and k[0].startswith("`")]
    return out + re.findall(r'"pytanie":\s*"([^"]+)"', tekst)


def Sprawdz(folder: str | Path) -> tuple[list[str], list[str]]:
    """Kontrola pytania.json przed złożeniem arkusza: (błędy, uwagi). Błędy trzeba poprawić
    (zły format, pytanie lub forma jak niedawno, fiszka dwa razy, mniej niż 3 formy); uwagi warto."""
    folder = _folder(folder)
    try:
        pyt = Pytania(folder)
    except (ValueError, FileNotFoundError) as e:
        return [str(e)], []
    bledy, uwagi = [], []
    fiszki = {f["nr"]: f for f in Fiszki()}
    dzien = _dzien_petla(folder)
    hist = Historia(bez_arkusza=folder.name)
    if len({p["fiszka"] for p in pyt}) < len(pyt):
        bledy.append("ta sama fiszka dwa razy na jednym arkuszu")
    if len(pyt) >= 4 and len({p["forma"] for p in pyt}) < 3:
        bledy.append(f"tylko {len({p['forma'] for p in pyt})} różne formy; na arkuszu co najmniej 3")
    for k, ile in Counter(_norm(p["kontekst"]) for p in pyt if p["kontekst"]).items():
        if ile > 2:
            bledy.append(f"kontekst „{k}” {ile} razy; najwyżej 2 pytania z jednego działu")
    for i, p in enumerate(pyt, 1):
        f = fiszki.get(p["fiszka"])
        if f is None:
            bledy.append(f"{i}: nie ma fiszki {p['fiszka']} w notatki/karty/reguly.md")
            continue
        if f["pokazana"][:10] == dzien.isoformat() and f["pokazana"] != folder.name:
            bledy.append(f"{i}: fiszka {f['nr']} była dziś na arkuszu {f['pokazana']}")
        hf = [h for h in hist if h["fiszka"] == f["nr"]]
        formy = _formy_do_wyboru(f, hf)
        if p["forma"] not in formy:
            dozwolone = FORMY_DLA_TYPU.get(f["typ"], tuple(FORMY))
            if p["forma"] in [h["forma"] for h in hf[-2:]]:
                bledy.append(f"{i}: fiszka {f['nr']} miała formę „{p['forma']}” w jednym z dwóch ostatnich pytań; "
                             f"wybierz z: {', '.join(formy)}")
            elif p["forma"] not in dozwolone:
                uwagi.append(f"{i}: forma „{p['forma']}” zwykle nie pasuje do typu {f['typ']} (pasują: {', '.join(dozwolone)})")
        if hf and p["kontekst"] and _norm(hf[-1]["kontekst"]) == _norm(p["kontekst"]):
            uwagi.append(f"{i}: ten sam kontekst co ostatnio przy tej fiszce ({hf[-1]['kontekst']}, {hf[-1]['data']})")
        t = _norm(p["pytanie"])
        for h in hist:
            r = SequenceMatcher(None, t, _norm(h["pytanie"])).ratio()
            if r >= PODOBNE:
                bledy.append(f"{i}: prawie to samo co pytanie {h['nr']} z arkusza {h['arkusz']} ({r:.0%} wspólnej treści)")
                break
        for wzor in _przyklady():
            if SequenceMatcher(None, t, _norm(wzor)).ratio() >= PODOBNE:
                bledy.append(f"{i}: to przykład z PROCEDURY.md#rozgrzewka, nie pytanie na arkusz")
                break
        for j, q in enumerate(pyt[: i - 1], 1):
            if SequenceMatcher(None, t, _norm(q["pytanie"])).ratio() >= PODOBNE:
                bledy.append(f"{i}: prawie to samo co pytanie {j} na tym arkuszu")
        if f["nastepna"] > dzien:
            uwagi.append(f"{i}: fiszka {f['nr']} przypada dopiero {f['nastepna']:%d.%m} (w porządku, gdy brak zaległych)")
        if len(p["pytanie"]) > MAKS_ZNAKOW:
            uwagi.append(f"{i}: {len(p['pytanie'])} znaków; w komórce arkusza mieści się ok. {MAKS_ZNAKOW}")
        if "zalicz" not in p["klucz"].lower():
            uwagi.append(f"{i}: klucz bez kryterium „Zalicz, gdy …”")
        if not p["kontekst"]:
            uwagi.append(f"{i}: brak „kontekst” (dział, z którego jest treść pytania)")
    return bledy, uwagi


def Pokaz(nry: list[int], folder: str | Path) -> None:
    """Zapisuje, że fiszki trafiły na arkusz w folderze (kolumna Pokazana), a pytania z pytania.json
    do dziennika notatki/karty/pytania.md (ponowny skład w tym samym folderze zastępuje jego wiersze)."""
    folder = _folder(folder)
    fiszki = Fiszki()
    for f in fiszki:
        if f["nr"] in nry:
            f["pokazana"] = folder.name
    _zapisz(fiszki)
    try:
        pyt = Pytania(folder)
    except (ValueError, FileNotFoundError):
        return
    _, wiersze = _dziennik()
    stare = {w["nr"]: w for w in wiersze if w["arkusz"] == folder.name}
    wiersze = [w for w in wiersze if w["arkusz"] != folder.name]
    for i, p in enumerate(pyt, 1):
        s = stare.get(str(i)) or {}
        wiersze.append({"data": _dzien_petla(folder).isoformat(), "arkusz": folder.name, "nr": str(i), "fiszka": str(p["fiszka"]),
                        "forma": p["forma"], "kontekst": p["kontekst"] or "—", "pytanie": p["pytanie"], "klucz": p["klucz"],
                        "wynik": s.get("wynik", "—") if s.get("pytanie") == p["pytanie"] else "—"})
    _zapisz_dziennik(wiersze)


def Ocen(folder: str | Path, wyniki: dict[int, bool], popraw: bool = False) -> str:
    """Ocena rozgrzewki z arkusza: {numer pytania: dobrze?}. Zmienia serię i termin fiszek,
    zapisuje rozgrzewka.json w folderze arkusza i wynik w dzienniku pytań. Druga ocena tego samego
    arkusza tylko z popraw=True (przywraca stan sprzed pierwszej oceny dla podanych pytań i ocenia od nowa)."""
    folder = _folder(folder)
    z = json.loads((folder / "zadania.json").read_text(encoding="utf-8"))
    mapa = {int(r["r"]): int(r["fiszka"]) for r in z.get("rozgrzewka") or []}
    if not mapa:
        raise ValueError(f"{folder.name}: arkusz bez rozgrzewki")
    zle = sorted(set(wyniki) - set(mapa))
    if zle:
        raise ValueError(f"{folder.name}: rozgrzewka ma pytania {min(mapa)}–{max(mapa)}, nie ma {zle}")
    plik = folder / "rozgrzewka.json"
    zapis = json.loads(plik.read_text(encoding="utf-8")) if plik.exists() else {"wyniki": {}, "przed": {}}
    if zapis["wyniki"] and not popraw:
        raise ValueError(f"{folder.name}: rozgrzewka już oceniona ({plik}); poprawka: popraw=True")
    fiszki = Fiszki()
    po_nr = {f["nr"]: f for f in fiszki}
    dzis = Dzis_petla()
    for r, dobrze in sorted(wyniki.items()):
        zapis["wyniki"][f"R{r}"] = bool(dobrze)
        f = po_nr.get(mapa[r])
        if f is None:
            continue  # fiszkę usunięto z pliku
        stan = zapis["przed"].get(str(f["nr"]))
        if stan:  # poprawka: wróć do stanu sprzed pierwszej oceny
            f["seria"], f["ostatnio"], f["nastepna"] = stan["seria"], _data_petla(stan["ostatnio"]), _data_petla(stan["nastepna"])
        else:
            zapis["przed"][str(f["nr"])] = {"seria": f["seria"], "ostatnio": f["ostatnio"].isoformat() if f["ostatnio"] else "—",
                                            "nastepna": f["nastepna"].isoformat()}
        f["seria"] = f["seria"] + 1 if dobrze else 0
        f["ostatnio"] = dzis
        f["nastepna"] = dzis + timedelta(days=INTERWALY[min(f["seria"], len(INTERWALY) - 1)])
    zapis["ocenione"] = datetime.now(TZ).isoformat(timespec="minutes")
    _zapisz(fiszki)
    plik.write_text(json.dumps(zapis, ensure_ascii=False, indent=1), encoding="utf-8")
    _, wiersze = _dziennik()
    dotyczy = [w for w in wiersze if w["arkusz"] == folder.name and f"R{w['nr']}" in zapis["wyniki"]]
    for w in dotyczy:
        w["wynik"] = "tak" if zapis["wyniki"][f"R{w['nr']}"] else "nie"
    if dotyczy:
        _zapisz_dziennik(wiersze)
    return _podsumowanie_rozgrzewki(folder) or ""


def _podsumowanie_rozgrzewki(folder: Path) -> str | None:
    z = json.loads((folder / "zadania.json").read_text(encoding="utf-8"))
    rozg = {f"R{r['r']}": r for r in z.get("rozgrzewka") or []}
    if not rozg:
        return None
    plik = folder / "rozgrzewka.json"
    wyniki = json.loads(plik.read_text(encoding="utf-8"))["wyniki"] if plik.exists() else {}
    if not wyniki:
        return f"Rozgrzewka ({len(rozg)} pytań): bez odpowiedzi."
    po_nr = {f["nr"]: f for f in Fiszki()}

    def opis(k: str) -> str:
        r = rozg[k]
        f = po_nr.get(int(r["fiszka"]))
        regula = r.get("jesli") or (f["jesli"] if f else "?")
        forma = FORMY.get(r.get("forma") or "", "")
        return f"pyt. {k[1:]} ({forma.lower() + ', ' if forma else ''}fiszka {r['fiszka']}: jeśli {regula})"

    nie = [k for k, v in wyniki.items() if not v and k in rozg]
    s = f"Rozgrzewka: {sum(wyniki.values())}/{len(wyniki)}"
    if nie:
        s += "; nie: " + "; ".join(opis(k) for k in nie)
    return s + "."


def Dodaj(jesli: str, to: str, przyklad: str = "", typ: str = "", dzial: str = "") -> int:
    """Nowa fiszka z reguły dziennika; wraca do rozgrzewki od jutra. Zwraca jej numer.
    Gdy fiszka o tym samym sygnale już jest, rzuca ValueError (wtedy Wroc(nr))."""
    jesli = re.sub(r"^\s*jeśli\s+", "", jesli.strip(), flags=re.I).rstrip(" ,")
    to = re.sub(r"^\s*to\s+", "", to.strip(), flags=re.I).rstrip(" .")
    if not jesli or not to:
        raise ValueError("fiszka potrzebuje sygnału (jeśli) i odruchu (to)")
    if typ and typ not in TYPY_petla:
        raise ValueError(f"typ musi być jednym z {TYPY_petla}")
    fiszki = Fiszki()
    klucz = lambda s: re.sub(r"\W+", " ", s.lower()).strip()  # noqa: E731
    for f in fiszki:
        if klucz(f["jesli"]) == klucz(jesli):
            raise ValueError(f"taka fiszka już jest: nr {f['nr']} (użyj Wroc({f['nr']}))")
    nr = max((f["nr"] for f in fiszki), default=0) + 1
    fiszki.append({"nr": nr, "jesli": jesli, "to": to, "przyklad": przyklad, "typ": typ, "dzial": dzial or "ogólne",
                   "seria": 0, "ostatnio": None, "nastepna": Dzis_petla() + timedelta(days=1), "pokazana": ""})
    _zapisz(fiszki)
    return nr


def Wroc(nr: int) -> None:
    """Błąd, na który jest fiszka, wrócił: seria od zera, fiszka od jutra w rozgrzewce przed innymi zaległymi
    (Ostatnio = dziś, bo to świeży dowód, że reguła nie działa)."""
    fiszki = Fiszki()
    for f in fiszki:
        if f["nr"] == int(nr):
            f["seria"], f["ostatnio"], f["nastepna"] = 0, Dzis_petla(), Dzis_petla() + timedelta(days=1)
            _zapisz(fiszki)
            return
    raise ValueError(f"brak fiszki nr {nr}")


# ---------------------------------------------------------------- pola z arkusza: czas, kontekst, bramki

def _minuty(godz: str) -> int:
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", godz or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"zła godzina „{godz}”: oczekiwane GG:MM")
    return 60 * int(m.group(1)) + int(m.group(2))


def Czasy(starty: dict[int, str], koniec: str | None = None) -> dict[int, int | None]:
    """Minuty na zadanie z godzin startu wpisanych pod zadaniami: do startu następnego zadania
    w kolejności rozwiązywania (nie numeracji), ostatnie do końca lekcji. Bez końca: None."""
    po_czasie = sorted(starty.items(), key=lambda kv: _minuty(kv[1]))
    granice = [_minuty(g) for _, g in po_czasie[1:]] + [_minuty(koniec) if koniec else None]
    wynik = {}
    for (nr, g), nastepny in zip(po_czasie, granice):
        wynik[nr] = None if nastepny is None else nastepny - _minuty(g)
    return dict(sorted(wynik.items()))


def Kontekst(folder: str | Path, **pola) -> dict:
    """Dane całej lekcji z pól rozpoczęcia i zakończenia arkusza do kontekst.json w folderze (scala z poprzednimi).
    Pola: start, koniec (GG:MM), sen (h), zm_start, zm_koniec (zmęczenie 1–5), najtrudniejsze, pytanie."""
    folder = _folder(folder)
    dozwolone = {"start", "koniec", "sen", "zm_start", "zm_koniec", "najtrudniejsze", "pytanie"}
    zle = set(pola) - dozwolone
    if zle:
        raise ValueError(f"nieznane pola {sorted(zle)}; dozwolone {sorted(dozwolone)}")
    for k in ("start", "koniec"):
        if pola.get(k):
            _minuty(pola[k])
    for k in ("zm_start", "zm_koniec"):
        if pola.get(k) is not None and not 1 <= int(pola[k]) <= 5:
            raise ValueError(f"{k}: zmęczenie 1–5, nie {pola[k]}")
    if pola.get("sen") is not None and not 0 <= float(pola["sen"]) <= 16:
        raise ValueError(f"sen: liczba godzin 0–16, nie {pola['sen']}")
    plik = folder / "kontekst.json"
    dane = json.loads(plik.read_text(encoding="utf-8")) if plik.exists() else {}
    dane.update({k: v for k, v in pola.items() if v not in (None, "")})
    dane["zapisano"] = datetime.now(TZ).isoformat(timespec="minutes")
    plik.write_text(json.dumps(dane, ensure_ascii=False, indent=1), encoding="utf-8")
    return dane


def _kontekst_lekcji(folder: Path) -> list[str]:
    plik = folder / "kontekst.json"
    if not plik.exists():
        return []
    k = json.loads(plik.read_text(encoding="utf-8"))
    czesci = []
    if k.get("start") or k.get("koniec"):
        s = f"lekcja {k.get('start', '?')}–{k.get('koniec', '?')}"
        if k.get("start") and k.get("koniec"):
            s += f" ({_minuty(k['koniec']) - _minuty(k['start'])} min)"
        czesci.append(s)
    if k.get("sen") is not None:
        czesci.append(f"sen {float(k['sen']):g} h".replace(".", ","))
    if k.get("zm_start") is not None or k.get("zm_koniec") is not None:
        czesci.append(f"zmęczenie {k.get('zm_start', '?')} → {k.get('zm_koniec', '?')} (skala 1–5)")
    out = ["Kontekst: " + " · ".join(czesci) + "."] if czesci else []
    if k.get("najtrudniejsze"):
        out.append(f"Najtrudniejsze według ucznia: {k['najtrudniejsze']}.")
    if k.get("pytanie"):
        out.append(f"Pytanie ucznia: {k['pytanie']} (odpowiedz w wątku).")
    return out


def Bramki(folder: str | Path, plan: dict[int, bool] | None = None, kontrola: dict[int, bool] | None = None,
           zapisz: bool = True) -> dict:
    """Sekcja „Bramki i kontekst” w WYNIK.md (uruchom po p.Wynik, który nadpisuje WYNIK.md).

    Czyta tagi z notatek prób ([pew:N], [min:N], [utk:…], [plan:…], [kon:…]; opis w nagłówku modułu),
    kontekst.json i rozgrzewka.json z folderu. plan i kontrola ({nr: tak?}) uzupełniają brakujące tagi.
    Wypisuje pewne błędy, kalibrację, tempo wobec matury, miejsca utknięcia, bramki, kontekst i rozgrzewkę."""
    folder = _folder(folder)
    plan, kontrola = plan or {}, kontrola or {}
    z = json.loads((folder / "zadania.json").read_text(encoding="utf-8"))
    od = _start_arkusza(folder)  # jak Wynik: próby od utworzenia arkusza
    pole = lambda v: "—" if v is None else str(v)  # noqa: E731
    wiersze, zadania = [], []
    con = sqlite3.connect(DB, timeout=30)
    try:
        for t in z.get("zadania", []):
            r = con.execute("SELECT result, note FROM history WHERE id = ? AND timestamp >= ? ORDER BY entry_id DESC LIMIT 1",
                            (t["id"], od)).fetchone()
            note = (r[1] or "") if r else ""
            pk, pew, mi, utk, pl, ko = (w.search(note) for w in (_PKT, _PEW, _MIN, _UTK, _PLAN, _KON))
            kon = ko.group(1).replace("-", "") if ko else {True: "WPSO", False: ""}.get(kontrola.get(t["nr"]))
            x = {"nr": t["nr"], "id": t["id"], "pew": int(pew.group(1)) if pew else None,
                 "pkt": float(pk.group(1)) if pk else None, "maks": int(pk.group(2)) if pk else None,
                 "min": int(mi.group(1)) if mi else None, "utk": utk.group(1) if utk else None,
                 "plan": (pl.group(1) == "tak") if pl else plan.get(t["nr"]), "kon": kon, "pomoc": "z pomocą" in note}
            x["komplet"] = x["pkt"] is not None and x["pkt"] >= x["maks"]
            zadania.append(x)
            punkty = f"{x['pkt']:g}/{x['maks']}" if pk else ("–" if not r else r[0])
            wiersze.append(f"| {t['nr']} | {t['id']} | {punkty}{' (pomoc)' if x['pomoc'] else ''} | {pole(x['pew'])} | "
                           f"{pole(x['min'])} | {UTKNIECIA.get(x['utk'], '—')} | {pole({True: 'tak', False: 'nie'}.get(x['plan']))} | "
                           f"{'—' if x['kon'] is None else (x['kon'] or 'brak')} |")
    finally:
        con.close()
    md = [SEKCJA, "", "| Nr | id | Punkty | Pewność | Min | Utknął | Plan | Kontrola |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |", *wiersze, ""]
    ocenione = [x for x in zadania if x["pew"] and x["pkt"] is not None]
    pewne_bledy = [x["nr"] for x in ocenione if x["pew"] == 3 and not x["komplet"]]
    if pewne_bledy:
        md.append(f"Pewne błędy (pewność 3, bez kompletu): zad. {', '.join(map(str, pewne_bledy))}. Omówione najpierw.")
    kal = {}
    for p in (3, 2, 1):
        grupa = [x for x in ocenione if x["pew"] == p]
        if grupa:
            kal[p] = (sum(x["komplet"] for x in grupa), len(grupa))
    if kal:
        md.append("Kalibracja (komplet punktów): " + "; ".join(f"pewność {p} → {a}/{b}" for p, (a, b) in kal.items()) + ".")
    elif zadania:
        md.append("Pewność: brak oznaczeń przy zadaniach.")
    z_czasem = [x for x in zadania if x["min"] is not None and x["maks"]]
    tempo = None
    if z_czasem:
        tempo = sum(x["min"] for x in z_czasem) / sum(x["maks"] for x in z_czasem)
        najdl = max(z_czasem, key=lambda x: x["min"] / x["maks"])
        md.append(f"Tempo: {_pl(tempo)} min/pkt (matura: {_pl(MIN_NA_PKT_MATURA)}); najdłużej na punkt: zad. {najdl['nr']} "
                  f"({najdl['min']} min za {najdl['maks']} pkt).")
    utk = {}
    for x in zadania:
        if x["utk"]:
            utk.setdefault(x["utk"], []).append(x["nr"])
    if utk:
        md.append("Utknął: " + "; ".join(f"{UTKNIECIA[k]} (zad. {', '.join(map(str, v))})" for k, v in utk.items()) + ".")
    bramki = []
    z_planem = [x for x in zadania if x["plan"] is not None]
    if z_planem:
        bramki.append(f"plan zapisany {sum(x['plan'] for x in z_planem)}/{len(z_planem)}")
    z_kontrola = [x for x in zadania if x["kon"] is not None]
    if z_kontrola:
        bramki.append(f"kontrola pełna (WPSO) {sum(len(set(x['kon'])) == 4 for x in z_kontrola)}/{len(z_kontrola)}, "
                      f"średnio {_pl(sum(len(set(x['kon'])) for x in z_kontrola) / len(z_kontrola))} z 4 kroków")
    if bramki:
        md.append("Bramki: " + " · ".join(bramki) + ".")
    md += _kontekst_lekcji(folder)
    rozg = _podsumowanie_rozgrzewki(folder)
    if rozg:
        md.append(rozg)
    sekcja = "\n".join(md).rstrip() + "\n"
    if zapisz:
        plik = folder / "WYNIK.md"
        if not plik.exists():
            raise FileNotFoundError(f"{plik}: najpierw p.Wynik('{folder.name}')")
        s = plik.read_text(encoding="utf-8")
        for naglowek in (SEKCJA, *_STARE_SEKCJE):
            wzor = re.compile(r"^" + re.escape(naglowek) + r"\n.*?(?=^## |\Z)", re.S | re.M)
            if wzor.search(s):
                s = wzor.sub(lambda _: sekcja + "\n", s, count=1)
                break
        else:
            s = s.rstrip("\n") + "\n\n" + sekcja
        plik.write_text(s.rstrip("\n") + "\n", encoding="utf-8")
    return {"zadania": zadania, "pewne_bledy": pewne_bledy, "kalibracja": kal, "tempo": tempo, "utkniecia": utk,
            "rozgrzewka": rozg, "sekcja": sekcja}


# ---------------------------------------------------------------- CLI

def _tak_nie(pary: list[str]) -> dict[int, bool]:
    wynik = {}
    for p in pary:
        k, _, v = p.partition(":")
        v = v.strip().lower()
        if not k.strip().lstrip("Rr").isdigit() or v not in ("tak", "nie", "t", "n", "1", "0"):
            raise SystemExit(f"Zły zapis „{p}”: oczekiwane NR:tak albo NR:nie")
        wynik[int(k.strip().lstrip("Rr"))] = v in ("tak", "t", "1")
    return wynik


def _wypisz(fiszki: list[dict], stan: bool = False) -> None:
    for f in fiszki:
        s = f"{f['nr']:>3}. Jeśli {f['jesli']}, to {f['to']}."
        if stan:
            s += f"  [seria {f['seria']}, następna {f['nastepna']:%d.%m}" + (f", pokazana {f['pokazana']}" if f["pokazana"] else "") + "]"
        print(s)


def _main_petla() -> int:
    ap = argparse.ArgumentParser(description="Pętla błędu: fiszki reguł, pytania rozgrzewki, pola z arkusza i ich odczyt.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("dzis", help="fiszki do powtórki dziś (domyślnie)")
    s.add_argument("-n", type=int, default=5)
    sub.add_parser("lista", help="wszystkie fiszki ze stanem")
    s = sub.add_parser("rozgrzewka", help="fiszki na lekcję: formy do wyboru i ostatnie pytania (PROCEDURY.md#rozgrzewka)")
    s.add_argument("folder")
    s.add_argument("-n", type=int, default=5)
    s = sub.add_parser("pytania", help="kontrola pytania.json przed złożeniem arkusza")
    s.add_argument("folder")
    s = sub.add_parser("ocen", help="ocena rozgrzewki z arkusza")
    s.add_argument("folder")
    s.add_argument("wyniki", nargs="+", help="NR:tak albo NR:nie, np. 1:tak 2:nie")
    s.add_argument("--popraw", action="store_true")
    s = sub.add_parser("dodaj", help="nowa fiszka")
    s.add_argument("jesli")
    s.add_argument("to")
    s.add_argument("--przyklad", default="")
    s.add_argument("--typ", default="")
    s.add_argument("--dzial", default="")
    s = sub.add_parser("wroc", help="błąd z fiszki wrócił")
    s.add_argument("nr", type=int)
    s = sub.add_parser("czasy", help="minuty na zadania z godzin startu")
    s.add_argument("starty", nargs="+", help="NR:GG:MM, np. 1:16:35 2:16:52")
    s.add_argument("--koniec", default=None, help="godzina końca lekcji GG:MM")
    s = sub.add_parser("kontekst", help="dane całej lekcji do kontekst.json")
    s.add_argument("folder")
    s.add_argument("--start")
    s.add_argument("--koniec")
    s.add_argument("--sen", type=float)
    s.add_argument("--zm-start", type=int)
    s.add_argument("--zm-koniec", type=int)
    s.add_argument("--najtrudniejsze")
    s.add_argument("--pytanie")
    s = sub.add_parser("bramki", help="sekcja Bramki i kontekst w WYNIK.md")
    s.add_argument("folder")
    s.add_argument("--plan", nargs="*", default=[], help="NR:tak/nie, gdy brak tagu [plan:…]")
    s.add_argument("--kontrola", nargs="*", default=[], help="NR:tak/nie, gdy brak tagu [kon:…]")
    a = ap.parse_args()
    try:
        if a.cmd in (None, "dzis"):
            dzis = Dzis_petla()
            fiszki = Wybierz(getattr(a, "n", 5))
            print(f"Rozgrzewka na {dzis:%d.%m.%Y} (zaległych: {sum(f['nastepna'] <= dzis for f in Fiszki())} z {len(Fiszki())}):")
            _wypisz(fiszki, stan=True)
        elif a.cmd == "lista":
            _wypisz(Fiszki(), stan=True)
        elif a.cmd == "rozgrzewka":
            folder = _folder(a.folder)
            print(f"Rozgrzewka {folder.name} (zasady: PROCEDURY.md#rozgrzewka). Na każdą fiszkę jedno nowe pytanie w jednej z podanych form;")
            print(f"zapis: {folder / 'pytania.json'}, potem: python3 matura.py petla pytania {folder.name}")
            for f in DoPytan(a.n, folder):
                print(f"\n[fiszka {f['nr']}] Jeśli {f['jesli']}, to {f['to']}.")
                print(f"    typ {f['typ']}, dział {f['dzial']}, seria {f['seria']}, następna {f['nastepna']:%d.%m}; źródło: {f['przyklad']}")
                print("    formy do wyboru: " + ", ".join(f"{k} ({FORMY[k]})" for k in f["formy"]))
                for h in f["historia"]:
                    print(f"    było {h['data']} ({h['arkusz']}): {h['forma']}, kontekst {h['kontekst']}, wynik {h['wynik']}: "
                          + h["pytanie"].replace("\n", " / ")[:100])
            if (folder / "pytania.json").exists():
                print("\nW folderze już jest pytania.json (fiszki wyżej są z niego).")
        elif a.cmd == "pytania":
            bledy, uwagi = Sprawdz(a.folder)
            for b in bledy:
                print(f"BŁĄD  {b}")
            for u in uwagi:
                print(f"uwaga {u}")
            if bledy:
                return 1
            print(f"OK: {len(Pytania(a.folder))} pytań gotowych do arkusza" + (" (przejrzyj uwagi)" if uwagi else ""))
        elif a.cmd == "ocen":
            print(Ocen(a.folder, _tak_nie(a.wyniki), popraw=a.popraw))
        elif a.cmd == "dodaj":
            print(f"Dodana fiszka nr {Dodaj(a.jesli, a.to, a.przyklad, a.typ, a.dzial)}")
        elif a.cmd == "wroc":
            Wroc(a.nr)
            print(f"Fiszka {a.nr}: seria 0, od jutra w rozgrzewce")
        elif a.cmd == "czasy":
            starty = {}
            for p in a.starty:
                nr, _, godz = p.partition(":")
                if not nr.isdigit():
                    raise ValueError(f"zły zapis „{p}”: oczekiwane NR:GG:MM")
                starty[int(nr)] = godz
            for nr, mi in Czasy(starty, a.koniec).items():
                print(f"zad. {nr}: " + (f"{mi} min -> [min:{mi}]" if mi is not None else "brak końca (podaj --koniec)"))
        elif a.cmd == "kontekst":
            print(json.dumps(Kontekst(a.folder, start=a.start, koniec=a.koniec, sen=a.sen, zm_start=a.zm_start,
                                      zm_koniec=a.zm_koniec, najtrudniejsze=a.najtrudniejsze, pytanie=a.pytanie),
                             ensure_ascii=False, indent=1))
        elif a.cmd == "bramki":
            print(Bramki(a.folder, _tak_nie(a.plan), _tak_nie(a.kontrola))["sekcja"])
    except (ValueError, FileNotFoundError) as e:
        print(f"Błąd: {e}", file=sys.stderr)
        return 1
    return 0


# ==============================================================================
# SEKCJA: arkusz (dawniej matura.py arkusz)
# ==============================================================================

#!/usr/bin/env python3
# Składa arkusz PDF z obrazów zadań z baza_obrazy/.
#
# Użycie:
#     python3 matura.py arkusz --ids 113 1353 80 --out arkusze/2026-10-20_1630/arkusz.pdf [--tytul "Lekcja 20.10"]
#         [--akceptuj 80] [--czas 75] [--regula "Dziedzina przed rachunkami"] [--regula "..."] [--powt 2312:2048]
#         [--rozgrzewka 5] [--bez-bramek] [--bez-pol] [--test]
#
# Każde zadanie: nagłówek "Zadanie N · maks. X pkt · id · nazwa", pod nim obraz z bazy
# (przycięte białe marginesy, pusta kratka i stopka "Strona x z y"). Obok PDF zapisuje
# zadania.json z kolejnością (numer na arkuszu -> id), potrzebne przy sprawdzaniu,
# oraz podglad/strona-N.png do obejrzenia każdej strony. Jeśli w folderze jest
# plan_arkusza.json (plan.Propozycja(..., folder=...)), rola, klucz i powtórka każdego
# zadania trafiają do zadania.json same (--powt nie jest wtedy potrzebne).
#
# Układ formularza (23.10.2026, prośby ucznia: minimalistycznie, jak dokument urzędowy; pola
# zwięzłe): nagłówek z numerem dokumentu, sekcje A rozpoczęcie lekcji (godzina, sen, zmęczenie),
# B rozgrzewka: pytania z pytania.json w folderze (układa je sesja arkusza według PROCEDURY.md#rozgrzewka,
# petla.Pytania), drukowane bez odpowiedzi, z polem na odpowiedź; C objaśnienia (trzy bramki
# oznaczenia, reguły dnia), D zadania
# z dwoma paskami pól pod każdym (plan, start, pomoc; pewność, kontrola W·P·S·O, utknąłem),
# E zakończenie (godzina, zmęczenie, najtrudniejsze, pytanie); stopka z numerem dokumentu
# i „Strona x z y”. Klucz rozgrzewki trafia tylko do zadania.json. Odczyt pól przy sprawdzaniu:
# matura.py petla. --rozgrzewka 0 wyłącza B (pełny arkusz próbny), --bez-bramek pomija plan i kontrolę,
# --bez-pol wszystkie pola do wpisywania, --test nie zapisuje stanu fiszek ani dziennika pytań.
#
# Kadry z bazy bywają ucięte na krawędzi. Ścinek poprzedniej strony u góry jest
# usuwany automatycznie. Tusz dotykający dolnej krawędzi zatrzymuje skrypt (kod 2)
# i zapisuje kontrola/<id>_dol.png: obejrzyj go. Jeśli na dole jest tylko śmieć
# (kratka, stopka, tabelka egzaminatora), podaj id w --akceptuj (cienki pas przy
# krawędzi zostanie usunięty). Jeśli ucięta jest treść, wymień zadanie.
# --rysunek ID: duża pustka w obrazie tego id, w której jest rysunek (ciemne linie w co najmniej
# 30% wierszy), nie jest traktowana jak pusta kratka (np. id 46, matura maj 2026, zad. 12).



FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MARGIN = 15 * mm
GAP = 8 * mm
PW, PH = A4
CW = PW - 2 * MARGIN
# Formularz: czerń, szarości, cienkie linie, szare pasma sekcji.
INK = HexColor("#111111")
SZARY = HexColor("#5C5C5C")
LINIA = HexColor("#8F8F8F")
LINIA_JASNA = HexColor("#C4C4C4")  # podziały wewnątrz pasków pól
PASMO = HexColor("#E9E9E9")
JASNE = HexColor("#F5F5F5")
SYSTEM = "System przygotowania do matury · matematyka, poziom rozszerzony"
# Trzy bramki z raportu o błędach (23.10.2026): plan, warunek przy ryzykownej operacji, kontrola.
BRAMKI = [
    ("Przed zadaniem", "Wypełnij pole „Plan”: cel, droga (twierdzenie, metoda), ramka (dziedzina, warunki). "
                       "2 minuty bez postępu: wróć do planu i zapisz drogę B."),
    ("W trakcie", "Przed dzieleniem przez wyrażenie z x, podniesieniem nierówności do kwadratu, opuszczeniem "
                  "modułu i przy parametrze najpierw zapisz warunek."),
    ("Przed oddaniem", "W – wzory z tablic (sprawdź x = 0), P – podstaw wynik, S – sens (0 ≤ P ≤ 1, rząd wielkości), "
                       "O – odpowiedź na pytanie."),
]
OZNACZENIA = ("Pewność: 1 – zgaduję, 2 – raczej dobrze, 3 – pewne. Utknąłem (tylko gdy zadanie stanęło): start – brak pomysłu, "
              "droga – w połowie, rachunki – w liczeniu, czas – czas lub zmęczenie. Pomoc: notatki, internet lub AI. "
              "Godziny jako GG:MM. Bez wydruku zapisz to samo skrótem przy zadaniu, np. „14:07 · p3 · WPSO · utk droga”.")
ZMECZENIE = ("Zmęczenie", "1 wypoczęty · 5 wyczerpany")


def bands(a: np.ndarray) -> list[tuple[int, int]]:
    """Pasy wierszy z tuszem, rozdzielone co najmniej 12 px pustki."""
    rows = np.flatnonzero(a.sum(axis=1) >= 2)
    if rows.size == 0:
        return []
    cuts = np.flatnonzero(np.diff(rows) >= 12)
    starts = np.r_[rows[0], rows[cuts + 1]]
    ends = np.r_[rows[cuts], rows[-1]]
    return list(zip(starts.tolist(), ends.tolist()))


def edges(img: Image.Image) -> tuple[bool, bool]:
    """(tusz przy górnej krawędzi, tusz przy dolnej krawędzi)."""
    a = np.asarray(img.convert("L")) < 120
    return a[:3].sum() > 5, a[-3:].sum() > 5


def ink(img: Image.Image) -> np.ndarray:
    """Maska tuszu bez linii kratki i tabel (długie linie poziome i pionowe)."""
    g = np.asarray(img.convert("L"))
    a = g < 120
    lines = g < 225  # linie wykrywamy na luźniejszym progu, bo kratka bywa szara
    h, w = a.shape
    # Poziome linie: cienkie (<= 4 px) pasy wierszy zajętych w ponad połowie szerokości.
    # Grubsze pasy to szare belki nagłówków "Zadanie N." i zostają.
    full = lines.sum(axis=1) > 0.5 * w
    y = 0
    while y < h:
        if full[y]:
            y2 = y
            while y2 + 1 < h and full[y2 + 1]:
                y2 += 1
            if y2 - y < 4:
                a[y : y2 + 1] = False
            y = y2 + 1
        else:
            y += 1
    # Kolumny z pionowym odcinkiem >= 40 px to linie kratki/tabeli, nie litery.
    run = np.zeros(w, dtype=int)
    best = np.zeros(w, dtype=int)
    for row in lines:
        run = np.where(row, run + 1, 0)
        best = np.maximum(best, run)
    a[:, best >= 40] = False
    return a


def trim(img: Image.Image, drop_bottom_edge: bool = False, rysunek: bool = False) -> Image.Image:
    """Przycina marginesy, ścinki przy krawędziach, pustą kratkę pod treścią, stopkę
    i tabelkę egzaminatora.

    Tusz to piksele ciemniejsze niż 120 (jasna kratka CKE ma ~190-210; ciemną
    kratkę starszych arkuszy usuwa ink())."""
    raw = np.asarray(img.convert("L")) < 120
    a = ink(img)
    h, w = a.shape
    b = bands(a)
    if not b:
        return img
    # Ścinek poprzedniej strony/zadania przy górnej krawędzi.
    if len(b) > 1 and b[0][0] <= 2 and b[0][1] - b[0][0] < 60:
        b = b[1:]
    if drop_bottom_edge and len(b) > 1 and b[-1][1] >= h - 3 and b[-1][1] - b[-1][0] < 60:
        b = b[:-1]
    # Wszystko za dużą pustką (pusta kratka) to stopka lub tabelka egzaminatora.
    uciete = False
    for i in range(1, len(b)):
        if b[i][0] - b[i - 1][1] >= 150:
            if rysunek and raw[b[i - 1][1] : b[i][0]].any(axis=1).mean() >= 0.3:
                continue  # jasny rysunek (ink() go nie widzi), nie kratka
            b = b[:i]
            uciete = True
            break
    # Stopka: ostatni pas niższy niż 40 px, oddzielony >= 60 px pustki. Po ucięciu
    # kratki stopka już odpadła; wtedy krótki pas wyrównany do lewej to polecenie
    # pod rysunkiem („Udowodnij, że …”, 20.10.2026), a wyśrodkowany to np. BRUDNOPIS.
    def lewy(p):
        c = np.flatnonzero(a[p[0] : p[1] + 1].any(axis=0))
        return c[0] if len(c) else w
    polecenie = uciete and len(b) > 1 and lewy(b[-1]) - min(lewy(p) for p in b[:-1]) < 40
    if not polecenie and len(b) > 1 and b[-1][0] - b[-2][1] >= 60 and b[-1][1] - b[-1][0] < 40:
        b = b[:-1]
    top, bottom = b[0][0], b[-1][1]
    cols = np.flatnonzero(raw[top : bottom + 1].sum(axis=0) >= 2)
    pad = 8
    return img.crop((max(cols[0] - pad, 0), max(top - pad, 0), min(cols[-1] + pad, w), min(bottom + pad, h)))


def punkty(t: str) -> int | None:
    try:
        sys.path.insert(0, str(BASE))
        pass  # jeden parser punktów dla całego systemu
        return _punkty_z_tekstu(t)
    except ImportError:  # brak scipy: stary, węższy parser
        m = re.search(r"\((\d+)\s*PKT", t, re.I) or re.search(r"\(0\s*[–-]\s*(\d+)\)", t)
        return int(m.group(1)) if m else None


def plan_sloty(folder: Path) -> dict[int, dict]:
    """id -> {rola, klucz, powt} z plan_arkusza.json (kandydat z dowolnej pozycji slotu)."""
    f = folder / "plan_arkusza.json"
    if not f.exists():
        return {}
    out = {}
    for s in json.loads(f.read_text(encoding="utf-8")).get("sloty", []):
        for k in s.get("kandydaci", []):
            out.setdefault(int(k), {"rola": s.get("rola"), "klucz": s.get("klucz"), "powt": s.get("powt")})
    return out


# ---------------------------------------------------------------- formularz

class Dokument(canvas.Canvas):
    """Canvas, który na końcu dorysowuje stopkę „Strona x z y” i nagłówek na stronach od drugiej."""

    def __init__(self, *args, stopka: str = "", naglowek: str = "", nr_dok: str = "", **kw):
        super().__init__(*args, **kw)
        self._strony: list[dict] = []
        self.stopka, self.naglowek, self.nr_dok = stopka, naglowek, nr_dok

    def showPage(self):  # noqa: N802 (nazwa z reportlab)
        self._strony.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        if self._code:  # ostatnia strona bez showPage
            self.showPage()
        n = len(self._strony)
        for i, stan in enumerate(self._strony, 1):
            self.__dict__.update(stan)
            self.setStrokeColor(LINIA)
            self.setLineWidth(0.4)
            self.line(MARGIN, MARGIN - 8, PW - MARGIN, MARGIN - 8)
            self.setFont("DV", 6.8)
            self.setFillColor(SZARY)
            self.drawString(MARGIN, MARGIN - 17, self.stopka)
            self.drawRightString(PW - MARGIN, MARGIN - 17, f"Strona {i} z {n}")
            if i > 1:
                self.drawString(MARGIN, PH - MARGIN + 4, self.naglowek)
                self.drawRightString(PW - MARGIN, PH - MARGIN + 4, self.nr_dok)
                self.line(MARGIN, PH - MARGIN + 0.5, PW - MARGIN, PH - MARGIN + 0.5)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)


def napis(c: canvas.Canvas, x: float, y: float, s: str, font: str = "DV", size: float = 8, kolor=INK, sp: float = 0.0) -> None:
    """Tekst z rozstrzeleniem liter. drawString zeruje charSpace po sobie; własny obiekt tekstu
    zostawiał rozstrzelenie w stanie strony i dalszy tekst wychodził poza komórki."""
    c.setFont(font, size)
    c.setFillColor(kolor)
    c.drawString(x, y, s, charSpace=sp)


def etykieta(c: canvas.Canvas, x: float, y: float, s: str, kolor=SZARY) -> None:
    """Etykieta pola: wersaliki 6,2 pt z rozstrzeleniem, jak w formularzach."""
    napis(c, x, y, s.upper(), "DV", 6.2, kolor, 0.3)


KRATKA, ODSTEP = 6.3, 6.5  # bok kratki do zaznaczenia, odstęp między opcjami
CYFRA = (8.5, 12)  # kratka na cyfrę w polu godziny


def kratki(c: canvas.Canvas, x: float, y: float, opisy: list[str], size: float = 7.3) -> float:
    """Kratki do zaznaczenia z podpisami w jednym wierszu (y = linia bazowa podpisów). Zwraca x za ostatnią."""
    c.setStrokeColor(INK)
    c.setLineWidth(0.6)
    c.setFillColor(INK)
    c.setFont("DV", size)
    for o in opisy:
        c.rect(x, y - 0.5, KRATKA, KRATKA, stroke=1, fill=0)
        x += KRATKA + (2.5 if o else 0)
        c.drawString(x, y, o)
        x += pdfmetrics.stringWidth(o, "DV", size) + ODSTEP
    return x - ODSTEP


def szer_kratek(opisy: list[str], size: float = 7.3) -> float:
    return sum(KRATKA + (2.5 if o else 0) + pdfmetrics.stringWidth(o, "DV", size) + ODSTEP for o in opisy) - ODSTEP


def grzebien(c: canvas.Canvas, x: float, yb: float) -> float:
    """Pole godziny GG:MM jak w formularzach: kratka na każdą cyfrę, dwukropek między parami (yb = dół kratek)."""
    w, h = CYFRA
    c.setFillColor(INK)
    for para in (0, 1):
        x0 = x + para * (2 * w + 7)
        c.setStrokeColor(INK)
        c.setLineWidth(0.6)
        c.rect(x0, yb, 2 * w, h, stroke=1, fill=0)
        c.setStrokeColor(LINIA)
        c.setLineWidth(0.4)
        c.line(x0 + w, yb, x0 + w, yb + h)
    c.setFont("DVB", 8)
    c.drawCentredString(x + 2 * w + 3.5, yb + 3, ":")
    return 4 * w + 7


def szer_etykiety(s: str) -> float:
    return pdfmetrics.stringWidth(s.upper(), "DV", 6.2) + 0.3 * len(s)


def _szer_tresci(k: dict) -> float:
    if k.get("opcje"):
        return szer_kratek(k["opcje"])
    if k.get("czas"):
        return 4 * CYFRA[0] + 7
    if k.get("jednostka"):
        return 26 + 3 + pdfmetrics.stringWidth(k["jednostka"], "DV", 7.3)
    return 0.0


def _szer_naglowka(k: dict) -> float:
    return max(szer_etykiety(k["et"]), pdfmetrics.stringWidth(k.get("pod") or "", "DV", 6.0))


def pasek(c: canvas.Canvas, y: float, komorki: list[dict], h: float) -> float:
    """Wiersz pól w jednej linii: etykieta z lewej (wersaliki, pod nią opcjonalnie szary podpis), obok zawartość:
    kratki (opcje), godzina GG:MM (czas), liczba z jednostką (jednostka) albo kropkowana linia do pisania (linia).
    w = część szerokości; komórki bez w dzielą resztę: linia bierze całą, pozostałe proporcjonalnie do treści.
    Zwraca y pod wierszem."""
    natur = [4 + _szer_naglowka(k) + 5.5 + _szer_tresci(k) + 5 for k in komorki]
    stale = sum(CW * k["w"] for k in komorki if k.get("w"))
    wolne = [i for i, k in enumerate(komorki) if not k.get("w")]
    szer = [CW * k["w"] if k.get("w") else 0.0 for k in komorki]
    pisanie = [i for i in wolne if komorki[i].get("linia")]
    if pisanie:
        for i in wolne:
            szer[i] = natur[i]
        szer[pisanie[0]] = CW - stale - sum(natur[i] for i in wolne if i != pisanie[0])
    elif wolne:
        suma = sum(natur[i] for i in wolne)
        for i in wolne:
            szer[i] = (CW - stale) * natur[i] / suma
    if stale + sum(natur[i] for i in wolne) > CW + 0.5:
        print("Uwaga: pasek pól szerszy niż strona: " + ", ".join(k["et"] for k in komorki), file=sys.stderr)
    c.setStrokeColor(LINIA)
    c.setLineWidth(0.5)
    c.rect(MARGIN, y - h, CW, h, stroke=1, fill=0)
    x, srodek = MARGIN, y - h / 2
    for i, k in enumerate(komorki):
        w = szer[i]
        if i:
            c.setStrokeColor(LINIA_JASNA)
            c.setLineWidth(0.5)
            c.line(x, y - h, x, y)
        if k.get("pod"):
            etykieta(c, x + 4, srodek + 1.3, k["et"])
            c.setFont("DV", 6.0)
            c.setFillColor(SZARY)
            c.drawString(x + 4, srodek - 6.2, k["pod"])
        else:
            etykieta(c, x + 4, srodek - 2.2, k["et"])
        xx = x + 4 + _szer_naglowka(k) + 5.5
        if k.get("opcje"):
            kratki(c, xx, srodek - 2.6, k["opcje"])
        elif k.get("czas"):
            grzebien(c, xx, srodek - CYFRA[1] / 2)
        elif k.get("jednostka"):
            c.setStrokeColor(INK)
            c.setLineWidth(0.6)
            c.rect(xx, srodek - 6, 26, 12, stroke=1, fill=0)
            c.setFont("DV", 7.3)
            c.setFillColor(INK)
            c.drawString(xx + 29, srodek - 2.6, k["jednostka"])
        elif k.get("linia"):
            c.setStrokeColor(LINIA)
            c.setLineWidth(0.5)
            c.setDash(0.6, 2.2)
            c.line(xx, y - h + 5.5, x + w - 5, y - h + 5.5)
            c.setDash()
        x += w
    c.setFillColor(INK)
    return y - h


def _linie(k: dict, w: float) -> tuple[list[str], list[str]]:
    tekst = simpleSplit(k["tekst"], "DVB" if k.get("mocny") else "DV", 7.5, w - 8) if k.get("tekst") else []
    dopisek = simpleSplit(k["dopisek"], "DV", 6.6, w - 8) if k.get("dopisek") else []
    return tekst, dopisek


def wys_rzedu(komorki: list[dict], h: float | None = None) -> float:
    """Wysokość wiersza: podana albo dopasowana do najdłuższego tekstu w komórkach (min. 22 pt)."""
    if h is not None:
        return h
    best = 22.0
    for k in komorki:
        tekst, dopisek = _linie(k, CW * k["w"])
        if tekst or dopisek:
            best = max(best, (18 if k.get("et") else 10) + 9.5 * len(tekst) + 8.5 * len(dopisek) + 2)
    return best


def rzad(c: canvas.Canvas, y: float, komorki: list[dict], h: float | None = None, tlo=None) -> float:
    """Wiersz komórek formularza na pełną szerokość. Komórka: dict(w=część szerokości, et=etykieta,
    opcje=[podpisy kratek], tekst=zawijany tekst 7,5 pt, mocny=pogrubienie, dopisek=szary tekst pod spodem).
    Zwraca y pod wierszem."""
    h = wys_rzedu(komorki, h)
    x = MARGIN
    for k in komorki:
        w = CW * k["w"]
        if tlo is not None:
            c.setFillColor(tlo)
            c.rect(x, y - h, w, h, stroke=0, fill=1)
        c.setStrokeColor(LINIA)
        c.setLineWidth(0.5)
        c.rect(x, y - h, w, h, stroke=1, fill=0)
        yy = y - 8
        if k.get("et"):
            etykieta(c, x + 3.5, yy, k["et"])
            yy -= 10
        tekst, dopisek = _linie(k, w)
        if tekst and not k.get("et"):
            yy -= 1
        c.setFillColor(INK)
        c.setFont("DVB" if k.get("mocny") else "DV", 7.5)
        for linia in tekst:
            c.drawString(x + 4, yy, linia)
            yy -= 9.5
        c.setFillColor(SZARY)
        c.setFont("DV", 6.6)
        for linia in dopisek:
            c.drawString(x + 4, yy + 1, linia)
            yy -= 8.5
        if k.get("opcje"):
            kratki(c, x + 4, y - h + 5.5, k["opcje"])
        x += w
    c.setFillColor(INK)
    return y - h


def sekcja(c: canvas.Canvas, y: float, litera: str, tytul: str, opis: str = "") -> float:
    """Szare pasmo sekcji „A.  ROZPOCZĘCIE LEKCJI”, z prawej krótki opis. Zwraca y pod pasmem."""
    h = 14
    c.setFillColor(PASMO)
    c.rect(MARGIN, y - h, CW, h, stroke=0, fill=1)
    napis(c, MARGIN + 5, y - h + 4.4, f"{litera}.  {tytul.upper()}", "DVB", 7.6, INK, 0.45)
    if opis:
        c.setFont("DV", 6.8)
        c.setFillColor(SZARY)
        c.drawRightString(MARGIN + CW - 5, y - h + 4.6, opis)
    c.setFillColor(INK)
    return y - h


def naglowek(c: canvas.Canvas, y: float, glowny: str, podtytul: str, nr_dok: str, metryka: list[tuple[str, str]]) -> float:
    """Nagłówek pierwszej strony: system, tytuł i lekcja z lewej, metryka dokumentu w ramce z prawej,
    pod spodem podwójna linia. Zwraca y pod linią."""
    napis(c, MARGIN, y - 7, SYSTEM.upper(), "DV", 6.2, SZARY, 0.4)
    c.setFillColor(INK)
    c.setFont("DVB", 17)
    c.drawString(MARGIN, y - 29, glowny)
    bw, h1, h2 = 196, 22, 22
    rozmiar = 9.5  # długi tytuł lekcji zmniejszamy, żeby nie wszedł na ramkę z prawej
    while rozmiar > 6.5 and pdfmetrics.stringWidth(podtytul, "DV", rozmiar) > CW - bw - 12:
        rozmiar -= 0.5
    c.setFont("DV", rozmiar)
    c.drawString(MARGIN, y - 43, podtytul)
    bx = MARGIN + CW - bw
    c.setStrokeColor(INK)
    c.setLineWidth(0.8)
    c.rect(bx, y - h1 - h2, bw, h1 + h2, stroke=1, fill=0)
    c.setStrokeColor(LINIA)
    c.setLineWidth(0.5)
    c.line(bx, y - h1, bx + bw, y - h1)
    etykieta(c, bx + 4, y - 8, "Nr dokumentu")
    c.setFont("DVB", 9)
    c.setFillColor(INK)
    c.drawString(bx + 4, y - 18.5, nr_dok)
    kol = bw / len(metryka)
    for i, (et, v) in enumerate(metryka):
        xx = bx + i * kol
        if i:
            c.line(xx, y - h1, xx, y - h1 - h2)
        etykieta(c, xx + 4, y - h1 - 8, et)
        c.setFont("DVB", 9)
        c.setFillColor(INK)
        c.drawString(xx + 4, y - h1 - 18.5, v)
    yb = y - h1 - h2 - 8
    c.setStrokeColor(INK)
    c.setLineWidth(1.2)
    c.line(MARGIN, yb, MARGIN + CW, yb)
    c.setLineWidth(0.4)
    c.line(MARGIN, yb - 2.2, MARGIN + CW, yb - 2.2)
    return yb - 2.2


def naglowek_zadania(c: canvas.Canvas, y: float, n: int, pkt: int | None, tid: int, name: str) -> float:
    """Linia i „Zadanie N · maks. X pkt”, z prawej id i nazwa źródła. Zwraca y pod nagłówkiem."""
    c.setStrokeColor(INK)
    c.setLineWidth(0.8)
    c.line(MARGIN, y, MARGIN + CW, y)
    c.setFillColor(INK)
    c.setFont("DVB", 10.5)
    c.drawString(MARGIN, y - 13.5, f"Zadanie {n}")
    if pkt:
        etykieta(c, MARGIN + pdfmetrics.stringWidth(f"Zadanie {n}", "DVB", 10.5) + 8, y - 13, f"maks. {pkt} pkt", INK)
    c.setFont("DV", 6.8)
    c.setFillColor(SZARY)
    c.drawRightString(MARGIN + CW, y - 13, f"id {tid} · {name}")
    c.setFillColor(INK)
    return y - 19


UTKNALEM = ["start", "droga", "rachunki", "czas"]  # te same słowa co w skrócie bez wydruku i w tagu [utk:…]


def metryczka(bramki: bool) -> list[tuple[list[dict], float]]:
    """Pola pod zadaniem (paski: komórki, wysokość): plan, start i pomoc, potem pewność, kontrola i utknięcie.
    Pełne znaczenia opcji są w „Oznaczeniach” na 1. stronie."""
    if bramki:
        return [([dict(et="Plan", pod="cel · droga · ramka", linia=True),
                  dict(et="Start", pod="godzina", czas=True),
                  dict(et="Pomoc", pod="notatki, AI", opcje=[""])], 23),
                ([dict(et="Pewność", opcje=["1 zgaduję", "2 raczej", "3 pewne"]),
                  dict(et="Kontrola", opcje=["W", "P", "S", "O"]),
                  dict(et="Utknąłem", opcje=UTKNALEM)], 16)]
    return [([dict(et="Start", pod="godzina", czas=True),
              dict(et="Pewność", opcje=["1", "2", "3"]),
              dict(et="Utknąłem", opcje=UTKNALEM),
              dict(et="Pomoc", pod="notatki, AI", opcje=[""])], 23)]


def _main_arkusz() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", type=int, nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tytul", default="")
    ap.add_argument("--akceptuj", type=int, nargs="*", default=[], help="id obejrzane: na dole tylko śmieć")
    ap.add_argument("--rysunek", type=int, nargs="*", default=[], help="id z rysunkiem w dużej pustce: nie ucinaj na niej")
    ap.add_argument("--regula", action="append", default=[], help="reguła z dziennika błędów na górę arkusza (do 3)")
    ap.add_argument("--czas", type=int, default=0, help="limit minut wypisany pod tytułem")
    ap.add_argument("--powt", nargs="*", default=[], help="powtórki jako ID:PIERWOTNE (trafia do zadania.json)")
    ap.add_argument("--rozgrzewka", type=int, default=5, help="0 = bez rozgrzewki; inaczej pytania z pytania.json w folderze (PROCEDURY.md#rozgrzewka)")
    ap.add_argument("--bez-bramek", action="store_true", help="bez planu i kontroli (symulacja egzaminu)")
    ap.add_argument("--bez-pol", action="store_true", help="bez pól do wpisywania (start, metryczki, zakończenie, samoocena)")
    ap.add_argument("--test", action="store_true", help="nie zapisuje stanu fiszek (notatki/karty/reguly.md) ani dziennika pytań (notatki/karty/pytania.md)")
    a = ap.parse_args()
    dup = sorted({i for i in a.ids if a.ids.count(i) > 1})
    if dup:
        print(f"To samo zadanie dwa razy: {dup}", file=sys.stderr)
        return 1

    out = Path(a.out)
    if not out.is_absolute():
        out = BASE / out
    out.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB)
    meta, pkt = {}, {}
    for tid in a.ids:
        r = conn.execute(
            'SELECT d.name, p.url, d.difficulty, d.latex FROM "desc" d LEFT JOIN photos p ON p.id = d.id WHERE d.id = ?',
            (tid,),
        ).fetchone()
        if not r:
            print(f"Brak zadania {tid}", file=sys.stderr)
            return 1
        meta[tid] = r[:3]
        pkt[tid] = punkty(r[3] or "")
    conn.close()

    images, bad = {}, []
    for tid in a.ids:
        name, url, _ = meta[tid]
        path = BASE / url if url else None
        if not path or not path.exists():
            print(f"Brak obrazu dla {tid}: {url}", file=sys.stderr)
            return 1
        img = Image.open(path).convert("L")
        top_edge, bottom_edge = edges(img)
        if top_edge:
            print(f"Uwaga {tid}: ścinek u góry usunięty automatycznie, sprawdź na podglądzie.", file=sys.stderr)
        if bottom_edge and tid not in a.akceptuj:
            k = out.parent / "kontrola"
            k.mkdir(exist_ok=True)
            img.crop((0, max(0, img.height - 200), img.width, img.height)).save(k / f"{tid}_dol.png")
            bad.append(tid)
        images[tid] = trim(img, drop_bottom_edge=tid in a.akceptuj, rysunek=tid in a.rysunek)
    if bad:
        print(
            "STOP: tusz przy dolnej krawędzi obrazu (możliwie ucięta treść): "
            + " ".join(map(str, bad))
            + f". Obejrzyj {out.parent / 'kontrola'}/<id>_dol.png; śmieć -> --akceptuj, ucięta treść -> wymień zadanie.",
            file=sys.stderr,
        )
        pf = out.parent / "plan_arkusza.json"
        if pf.exists():
            for s in json.loads(pf.read_text(encoding="utf-8")).get("sloty", []):
                for tid in bad:
                    if tid in s.get("kandydaci", []):
                        zapas = [k for k in s["kandydaci"] if k not in a.ids]
                        print(f"Zapas dla {tid} ({s.get('rola')}, {s.get('klucz')}): {zapas}", file=sys.stderr)
        return 2

    pdfmetrics.registerFont(TTFont("DV", FONT))
    pdfmetrics.registerFont(TTFont("DVB", FONT_B))
    _dt = datetime.now(TZ)
    tytul = a.tytul or f"Arkusz {('pon','wt','śr','czw','pt','sob','nd')[_dt.weekday()]} {_dt:%d.%m.%Y}"
    glowny = "Arkusz próbny" if "próbny" in tytul.lower() else "Arkusz ćwiczeniowy"
    # „Arkusz próbny wt 10.11.2026” pod tytułem „Arkusz próbny” powtarzałby nazwę.
    podtytul = re.sub(r"^\s*arkusz\s+próbny[\s·:,–-]*", "", tytul, flags=re.I) or f"{_dt:%d.%m.%Y}"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})_(\d{4})", out.parent.name)
    nr_dok = f"ARK-{m[1]}-{m[2]}-{m[3]}-{m[4]}" if m else f"ARK-{_dt:%Y-%m-%d-%H%M}"
    pola, bramki = not a.bez_pol, not a.bez_bramek
    c = Dokument(str(out), pagesize=A4, stopka=f"{nr_dok} · wygenerowano {_dt:%d.%m.%Y, %H:%M}",
                 naglowek=f"{glowny} · {podtytul}", nr_dok=nr_dok)
    c.setTitle(tytul)
    c.setAuthor("System przygotowania do matury")
    c.setSubject(nr_dok)

    gora_dalsze, dol = PH - MARGIN - 10, MARGIN + 2
    y = PH - MARGIN

    def miejsce(potrzeba: float) -> None:
        """Nowa strona, gdy do dołu zostało mniej niż potrzeba."""
        nonlocal y
        if y - potrzeba < dol:
            c.showPage()
            y = gora_dalsze

    znane = [pkt[t] for t in a.ids if pkt[t]]
    metryka = [("Zadania", str(len(a.ids))), ("Punkty", str(sum(znane)) if len(znane) == len(a.ids) else "—"),
               ("Czas", f"{a.czas} min" if a.czas else "—")]
    y = naglowek(c, y, glowny, podtytul, nr_dok, metryka) - 10

    pytania = []
    if a.rozgrzewka > 0:
        try:  # rozgrzewka nie może zatrzymać arkusza
            sys.path.insert(0, str(BASE))
            pass
            pytania = petla.Pytania(out.parent)
        except Exception as e:  # noqa: BLE001
            print(f"Uwaga: rozgrzewka pominięta ({type(e).__name__}: {e}). Pytania układa się według PROCEDURY.md#rozgrzewka.",
                  file=sys.stderr)

    litery = iter("ABCDEFGH")
    if pola:
        y = sekcja(c, y, next(litery), "Rozpoczęcie lekcji", "wypełnij przed rozgrzewką")
        y = pasek(c, y, [dict(et="Godzina", pod="rozpoczęcia", czas=True), dict(et="Sen", pod="ostatniej nocy", jednostka="h"),
                         dict(et=ZMECZENIE[0], pod=ZMECZENIE[1], opcje=["1", "2", "3", "4", "5"])], 23) - 10
    if pytania:
        # Same pytania, bez odpowiedzi; klucz idzie do zadania.json dla sprawdzania.
        wiersze = []
        for i, q in enumerate(pytania, 1):
            tresc = dict(et=petla.FORMY[q["forma"]], tekst=q["pytanie"])
            wiersze.append([dict(w=0.06, tekst=str(i), mocny=True),
                            *([dict(w=0.56, **tresc), dict(w=0.38)] if pola else [dict(w=0.94, **tresc)])])
        naglowki = [dict(w=0.06, et="Nr"), *([dict(w=0.56, et="Pytanie"), dict(w=0.38, et="Odpowiedź")]
                                             if pola else [dict(w=0.94, et="Pytanie")])]
        wysokosci = [max(wys_rzedu(w), 32) if pola else wys_rzedu(w) for w in wiersze]
        miejsce(14 + 13 + sum(wysokosci))
        y = sekcja(c, y, next(litery), "Rozgrzewka", "5 minut, bez notatek · odpowiedzi omówimy po sprawdzeniu")
        y = rzad(c, y, naglowki, h=13, tlo=JASNE)
        for w, h in zip(wiersze, wysokosci):
            y = rzad(c, y, w, h)
        y -= 10
    objasnienia = [([dict(w=1 / 3, et=et, tekst=t) for et, t in BRAMKI], None)] if bramki else []
    if pola:
        objasnienia.append(([dict(w=1.0, et="Oznaczenia", tekst=OZNACZENIA)], None))
    if a.regula:
        objasnienia.append(([dict(w=1.0, et="Reguły z dziennika błędów", tekst="\n".join(f"• {r}" for r in a.regula[:3]))], None))
    if objasnienia:
        miejsce(14 + sum(wys_rzedu(k, h) for k, h in objasnienia))
        y = sekcja(c, y, next(litery), "Objaśnienia", "trzy bramki: plan, warunek, kontrola" if bramki else "")
        for k, h in objasnienia:
            y = rzad(c, y, k, h)
        y -= 10

    sloty = plan_sloty(out.parent)
    powt = {i: s["powt"] for i, s in sloty.items() if s.get("powt")}
    powt.update({int(x.split(":")[0]): int(x.split(":")[1]) for x in a.powt})
    pola_zadania = metryczka(bramki) if pola else []
    h_pol = sum(h for _, h in pola_zadania)
    litera_zadan = next(litery)
    manifest = []
    for n, tid in enumerate(a.ids, 1):
        name, url, diff = meta[tid]
        img = images[tid]
        iw, ih = img.size
        maks_h = gora_dalsze - dol - 19 - 3 * mm - (h_pol + 2.5 * mm if pola else 0) - (14 if n == 1 else 0)
        scale = min(CW / iw, maks_h / ih, 0.5)  # 0.5 pt/px ~ czytelny rozmiar druku
        dw, dh = iw * scale, ih * scale
        need = 19 + 3 * mm + dh + (2.5 * mm + h_pol if pola else 0)
        if n == 1:
            miejsce(14 + 6 + need)
            y = sekcja(c, y, litera_zadan, "Zadania", "pola pod zadaniem wypełniaj w trakcie, kilka sekund" if pola else "") - 6
        else:
            miejsce(need)
        y = naglowek_zadania(c, y, n, pkt[tid], tid, name) - 3 * mm
        # drawImage (XObject), nie drawInlineImage: duże obrazy inline psują się w przeglądarkach mobilnych.
        c.drawImage(ImageReader(img), MARGIN, y - dh, dw, dh)
        y -= dh
        if pola:
            y -= 2.5 * mm
            for komorki, h in pola_zadania:
                y = pasek(c, y, komorki, h)
        y -= GAP
        manifest.append({"nr": n, "id": tid, "nazwa": name, "trudnosc": round(diff or 0, 2), "pkt": pkt[tid], "powt": powt.get(tid),
                         "rola": sloty.get(tid, {}).get("rola"), "klucz": sloty.get(tid, {}).get("klucz"), "obraz": url})
    if pola:
        koniec = [([dict(et="Godzina", pod="zakończenia", czas=True),
                    dict(et=ZMECZENIE[0], pod=ZMECZENIE[1], opcje=["1", "2", "3", "4", "5"])], 23),
                  ([dict(et="Najtrudniejsze", pod="dziś, jedno zdanie", linia=True)], 25),
                  ([dict(et="Pytanie", pod="do korepetytora, opcjonalnie", linia=True)], 25)]
        miejsce(14 + sum(h for _, h in koniec))
        y = sekcja(c, y, next(litery), "Zakończenie lekcji", "wypełnij po ostatnim zadaniu")
        for komorki, h in koniec:
            y = pasek(c, y, komorki, h)
    c.save()

    (out.parent / "zadania.json").write_text(
        json.dumps({"tytul": tytul, "pdf": out.name, "nr_dokumentu": nr_dok, "czas": a.czas or None, "reguly": a.regula[:3],
                    "rozgrzewka": [{"r": i, **q} for i, q in enumerate(pytania, 1)],
                    "bramki": bramki, "pola": pola, "zadania": manifest}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    if pytania and not a.test:
        try:
            petla.Pokaz([q["fiszka"] for q in pytania], out.parent)
        except Exception as e:  # noqa: BLE001
            print(f"Uwaga: nie zapisano pokazanych fiszek i dziennika pytań ({type(e).__name__}: {e})", file=sys.stderr)
    prev = out.parent / "podglad"
    prev.mkdir(exist_ok=True)
    for old in prev.glob("strona-*.png"):
        old.unlink()
    subprocess.run(["pdftoppm", "-r", "70", "-png", str(out), str(prev / "strona")], check=False)
    print(out)
    print("Podgląd stron:", " ".join(str(p) for p in sorted(prev.glob("strona-*.png"))))
    return 0


# ==============================================================================
# SEKCJA: raport (dawniej matura.py raport)
# ==============================================================================

# Tygodniowy raport postępów ucznia: gęsty PDF z LaTeX-a z wykresami 2D i 3D.
#
# Uruchomienie (piątek 3:00, zaplanowane zadanie; procedura w PROCEDURY.md#raport):
#     python3 matura.py raport                  # raport z prawdziwej historii, dziś
#     python3 matura.py raport --komentarz K.md # z akapitem komentarza (plain text, $...$ wolno)
#     python3 matura.py raport --symulacja      # test na symulowanym uczniu (kopia bazy w /tmp)
#     python3 matura.py raport --dzien 2026-11-21 --symulacja
#
# Wynik: raporty/RRRR-MM-DD_tydzien/ (symulacja: RRRR-MM-DD_tydzien_sym/) z raport.pdf, raport.tex, dane.json,
# wykresy/*.pdf i podglad/strona-N.png. Skrypt sam doinstaluje LaTeX (raporty/instaluj.sh) i biblioteki Pythona.
# Niczego nie zapisuje do matura.db: tylko czyta. Symulacja pracuje na kopii bazy w katalogu tymczasowym.
# Nie zmienia matura.py ani plan.py (ich właścicielem jest wątek „Ulepszenie kodu i narzędzi”).


RAPORTY = BASE / "raporty"
DNI_PL = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]
MIES_PL = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
           "października", "listopada", "grudnia"]

# Paleta (dataviz, wariant jasny: raport jest do druku i czytania na telefonie)
NIEBIESKI, POMARANCZ, AQUA, ZOLTY, MAGENTA, ZIELONY, FIOLET, CZERWONY = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
KATEG = [NIEBIESKI, POMARANCZ, AQUA, ZOLTY, MAGENTA, ZIELONY, FIOLET, CZERWONY]
STATUS = {"mocne": "#0ca30c", "niepewne": "#fab219", "slabe": "#d03b3b"}
ETYKIETA = {"mocne": "mocne", "niepewne": "za mało danych", "slabe": "słabe"}
INK_raport, INK2, MUTED, GRID, AXIS, SURF = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"


# ============================================================================ zależności

def zapewnij_zaleznosci() -> None:
    """LaTeX (apt) i matplotlib/scipy (pip). Kontener startuje bez nich, więc sprawdzamy przy każdym starcie."""
    if not shutil.which("pdflatex"):
        print("[raport] brak pdflatex: instaluję TeX Live (ok. 1–2 min)…", flush=True)
        subprocess.run(["bash", str(DANE / "instaluj_tex.sh")], check=True)
    brak = [p for p in ("matplotlib", "scipy", "numpy", "seaborn") if importlib.util.find_spec(p) is None]
    if brak:
        print(f"[raport] instaluję {brak}…", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *brak], check=True)


# ============================================================================ symulacja

def przygotuj_symulacje(dzien: date, ziarno: int = 7) -> Path:
    """Kopia systemu w katalogu tymczasowym z symulowanym uczniem od 19.10.2026 do dnia przed `dzien`."""
    import numpy as np
    kat = Path(tempfile.mkdtemp(prefix="raport_sym_"))
    for f in ("matura.py", "matura.db", "PROCEDURY.md"):
        if (BASE / f).exists():
            shutil.copy2(BASE / f, kat / f)
    shutil.copytree(DANE, kat / "dane")
    shutil.copytree(NOTATKI, kat / "notatki")
    (kat / "arkusze").mkdir()
    con = sqlite3.connect(kat / "matura.db")
    con.execute("DELETE FROM history")
    con.execute("DELETE FROM done")
    con.execute("DELETE FROM cooldown_fails")
    con.commit()
    con.close()

    sys.path.insert(0, str(kat))
    sys.modules.pop("matura", None)   # CLI zarejestrował tu bieżący plik; symulacja ma załadować kopię
    import matura as m  # noqa: E402  (z kopii)
    p = m    # noqa: E402
    e = m._engine
    rng = np.random.default_rng(ziarno)
    dzialy = sorted({d["makro_dzial"] for d in e.dims_list if d.get("segment") == "A"})
    umiej = {d: rng.normal(0.0, 1.0) for d in dzialy}
    # symulowany uczeń: słabszy z kilku działów
    for d, v in (("Analiza i stochastyka", -1.2), ("Trygonometria", -0.9), ("Ciągi liczbowe", -0.4)):
        umiej[d] = v
    pula = defaultdict(list)
    for tid in e.ids.tolist():
        k = e.id_to_idx[tid]
        if e.norms[k] < 1.5 or e.norms[k] > 6.5:
            continue
        pk = m.Punkty(tid)
        if not pk or pk < 2 or pk > 6:
            continue
        pula[p.Dzial(tid)].append((tid, pk))
    opisy = {"rachunkowy": "błąd w przekształceniu", "pojęciowy": "pomylił definicję", "metoda": "zła metoda",
             "warunki": "pominął dziedzinę", "niedokończone": "brak odpowiedzi końcowej", "odczyt": "źle odczytał dane",
             "zapis": "niejasny zapis"}
    wiersze, uzyte, fails = [], set(), []
    lekcje = {0: ["16:30"], 1: ["14:00", "16:30"], 3: ["13:00", "16:30"]}
    d = date(2026, 10, 19)
    while d < dzien:
        for g in lekcje.get(d.weekday(), []):
            t = p.Tydzien(d)
            dz_bloku = [p.Dzial_klucza(k) for k in t["klucze"]] or dzialy
            tydz = (d - date(2026, 10, 19)).days / 7
            ts0 = datetime.fromisoformat(f"{d}T{g}").replace(tzinfo=TZ).timestamp() + 3 * 3600
            for j in range(4):
                powt = None
                if j == 0 and fails and (d - fails[0][1]).days >= 3:
                    powt, _, dz = fails.pop(0)
                else:
                    dz = dz_bloku[j % len(dz_bloku)] if rng.random() < 0.75 else rng.choice(dzialy)
                kand = [x for x in pula.get(dz, []) if x[0] not in uzyte] or [x for x in pula[rng.choice(list(pula))]]
                tid, pk = kand[int(rng.integers(len(kand)))]
                uzyte.add(tid)
                n = float(e.norms[e.id_to_idx[tid]])
                ucz = umiej[dz] + 0.12 * tydz * (1 if dz in dz_bloku else 0.3)
                pr = 1 / (1 + math.exp(-(1.6 + ucz - 0.8 * (n - 3.5))))
                pkt = int(rng.binomial(pk, pr))
                wynik = "ok" if pkt == pk else ("fail" if pkt == 0 else "partial")
                typ = "" if wynik == "ok" else rng.choice(TYPY, p=[.3, .15, .15, .15, .1, .1, .05])
                note = f"[pkt: {round(10 * pkt / pk, 1):g}/10] " + (f"[{typ}] {opisy[typ]}" if typ else "czysto") + f"; {pkt}/{pk} pkt"
                if powt:
                    note += f"; powt:{powt}"
                if wynik == "fail" and not powt:
                    fails.append((tid, d, dz))
                wiersze.append((tid, wynik, "SYMULACJA " + note, 1, ts0 + 600 * j))
        d += timedelta(days=1)
    con = sqlite3.connect(kat / "matura.db")
    con.executemany("INSERT INTO history (id, result, note, attempt, timestamp) VALUES (?,?,?,?,?)", wiersze)
    con.executemany("INSERT OR REPLACE INTO done (id, result, note, attempt, timestamp) VALUES (?,?,?,?,?)", wiersze)
    con.commit()
    con.close()
    # dwa pełne arkusze w postep.md kopii
    pm = kat / "notatki" / "postep.md"
    tekst = pm.read_text(encoding="utf-8")
    wiersz = "| --- | --- | --- | --- | --- | --- | --- |\n"
    dodaj = ""
    if dzien > date(2026, 10, 27):
        dodaj += "| 27.10.2026 | CKE_2025_Z01 | 24 | 48% | 175 | trygonometria, prawdopodobieństwo | symulacja |\n"
    if dzien > date(2026, 12, 15):
        dodaj += "| 15.12.2026 | CKE_2025_Z02 | 31 | 62% | 170 | stereometria | symulacja |\n"
    pm.write_text(tekst.replace(wiersz, wiersz + dodaj, 1), encoding="utf-8")
    print(f"[raport] symulacja: {len(wiersze)} prób w {kat}", flush=True)
    return kat


# ============================================================================ dane

def _tex(s) -> str:
    s = str(s)
    zam = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
           "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(zam.get(c, c) if _znak_tex_ok(c) else _znak_zastepczy(c) for c in s)


_ZNAKI_TEX: set[str] = set()


def _znak_tex_ok(c: str) -> bool:
    """Czy pdflatex (T1 + utf8 + deklaracje z PREAMBULA) zna znak. Nieznany znak przerywał cały raport."""
    if not _ZNAKI_TEX:
        _ZNAKI_TEX.update(chr(int(h, 16)) for h in re.findall(r"DeclareUnicodeCharacter\{([0-9A-F]+)\}", PREAMBULA))
        _ZNAKI_TEX.update("–—„”“‘’…•€")
    return ord(c) < 0x180 or c in _ZNAKI_TEX


def _znak_zastepczy(c: str) -> str:
    z = unicodedata.normalize("NFKC", c)
    return z if z != c and all(_znak_tex_ok(x) for x in z) else "?"


def _tex_z_math(s: str) -> str:
    """Tekst z komentarza: zwykłe znaki escapowane, fragmenty $...$ zostają jako LaTeX."""
    czesci = re.split(r"(\$[^$]+\$)", s)
    return "".join(c if c.startswith("$") and c.endswith("$") and len(c) > 2 else _tex(c) for c in czesci)


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def _data_pl(d: date) -> str:
    return f"{d.day} {MIES_PL[d.month - 1]} {d.year}"


def zbierz(m, p, teraz: float) -> dict:
    import numpy as np
    e = m._engine
    dzis = _dt(teraz).date()
    con = sqlite3.connect(Path(m.DB))
    rows = con.execute("SELECT entry_id, id, result, note, timestamp FROM history WHERE timestamp <= ? ORDER BY entry_id",
                       (teraz,)).fetchall()
    nazwy = dict(con.execute("SELECT id, name FROM desc").fetchall())
    con.close()
    proby = []
    for eid, tid, res, note, ts in rows:
        note = note or ""
        if tid not in e.id_to_idx:
            continue
        pk = re.search(r";\s*([0-9.]+)/(\d+) pkt", note)
        typ = re.search(r"\[(" + "|".join(TYPY) + r")\]", note)
        opis = re.sub(r"^\s*(SYMULACJA\s*)?(\[[^\]]*\]\s*)*", "", note).split(";")[0].strip()
        proby.append({"id": int(tid), "nazwa": nazwy.get(tid, ""), "wynik": res, "note": note,
                      "ts": float(ts), "sc": float(m._extract_task_score(res, note)),
                      "pkt": float(pk.group(1)) if pk else None, "maks": int(pk.group(2)) if pk else None,
                      "typ": typ.group(1) if typ else None, "opis": opis,
                      "powt": bool(re.search(r"powt:\d+", note)),
                      "dzial": p.Dzial(tid), "norma": float(e.norms[e.id_to_idx[tid]])})
    t7, t14 = teraz - 7 * 86400, teraz - 14 * 86400
    tyg = [x for x in proby if x["ts"] > t7]
    pop = [x for x in proby if t14 < x["ts"] <= t7]

    def srednia(xs):
        return round(10 * float(np.mean([x["sc"] for x in xs])), 1) if xs else None

    def punkty(xs):
        a = [x for x in xs if x["maks"]]
        return (sum(x["pkt"] for x in a), sum(x["maks"] for x in a))

    h = p.Harmonogram()
    tw = p.Tydzien(dzis)
    nast = p.Tydzien(dzis + timedelta(days=(7 - dzis.weekday()) % 7 or 7))
    matura_dt = datetime.fromisoformat(h["matura"]).replace(tzinfo=TZ)
    prof = m.Profil(proby=[(x["id"], x["sc"] / 10, x["ts"]) for x in proby], teraz=teraz)
    prof_pop = m.Profil(proby=[(x["id"], x["sc"] / 10, x["ts"]) for x in proby if x["ts"] <= t7], teraz=t7)
    zmiana = {d["dzial"]: d["opanowanie"] for d in prof_pop.get("dzialy", [])}
    fit = m._dopasuj([(x["id"], x["sc"] / 10, x["ts"]) for x in proby], teraz) if proby else None
    kraj = na_tle_kraju(m, p, proby, fit)
    try:
        powt = p.Powtorki(dzis)
    except Exception:
        powt = []
    # pełne arkusze z postep.md
    pelne = []
    pm = Path(p.NOTATKI) / "postep.md"
    if pm.exists():
        sek = pm.read_text(encoding="utf-8").split("## Pełne arkusze")[-1].split("##")[0]
        for lin in sek.splitlines():
            c = [x.strip() for x in lin.strip().strip("|").split("|")]
            if len(c) >= 4 and re.match(r"\d{1,2}\.\d{1,2}\.\d{4}", c[0]):
                proc = re.search(r"(\d+)", c[3])
                pelne.append({"data": datetime.strptime(c[0], "%d.%m.%Y").date().isoformat(), "zestaw": c[1],
                              "pkt": c[2], "proc": int(proc.group(1)) if proc else None,
                              "czas": c[4] if len(c) > 4 else "", "stracone": c[5] if len(c) > 5 else ""})
    # arkusze z tygodnia
    ark = []
    for f in sorted((Path(p.ARKUSZE)).glob("*/zadania.json")):
        try:
            d = datetime.strptime(f.parent.name[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if (dzis - d).days <= 7 and d <= dzis:
            z = json.loads(f.read_text(encoding="utf-8"))
            ids = [x["id"] for x in z.get("zadania", [])]
            zrob = {x["id"] for x in proby if x["ts"] >= _start_arkusza(f.parent)}
            ark.append({"folder": f.parent.name, "zadan": len(ids), "sprawdzone": sum(i in zrob for i in ids),
                        "wynik": (f.parent / "WYNIK.md").exists()})
    # 3D: sfera z harmonik sferycznych (jak Opanowanie, ostatnia próba każdego zadania)
    ost = {}
    for x in proby:
        ost[x["id"]] = x
    sfera = None
    if len(ost) >= 3:
        pts = np.array([max(x["sc"], m.MIN_RADIUS) * m.project_160_to_3d(e.vectors[e.id_to_idx[i]]) for i, x in ost.items()],
                       dtype=np.float32)
        terms, coeffs, st = m.fit_spherical_harmonics(pts)
        try:
            kryt = m.find_critical_points(terms, coeffs, top_tags=3)
        except Exception:
            kryt = []
        sfera = {"terms": terms, "coeffs": coeffs, "stats": st, "kryt": kryt,
                 "pkt": [(m.project_160_to_3d(e.vectors[e.id_to_idx[i]]), x["sc"]) for i, x in ost.items()]}
    dni_akt = sorted({_dt(x["ts"]).date() for x in tyg})
    gotowosc = None
    if fit is not None:
        b = fit["beta"]
        gotowosc = round(float(np.mean([100 / (1 + math.exp(-(b[0] + b[2 + k]))) for k in range(fit["nD"])])))
    return {
        "teraz": teraz, "dzis": dzis, "od": _dt(t7).date(), "proby": proby, "tyg": tyg, "pop": pop,
        "kpi": {"proby_tyg": len(tyg), "proby_pop": len(pop), "sr_tyg": srednia(tyg), "sr_pop": srednia(pop),
                "pkt_tyg": punkty(tyg), "ok": sum(x["wynik"] == "ok" for x in tyg),
                "partial": sum(x["wynik"] == "partial" for x in tyg), "fail": sum(x["wynik"] == "fail" for x in tyg),
                "lacznie": len(proby), "zadan": len(ost), "dni_akt": len(dni_akt),
                "dni_do_matury": (matura_dt.date() - dzis).days, "powtorki": len(powt), "gotowosc": gotowosc,
                "sr_calosc": srednia(proby)},
        "harm": h, "tydz": tw, "nast": nast, "profil": prof, "zmiana": zmiana, "fit": fit, "powt": powt,
        "pelne": pelne, "arkusze": ark, "sfera": sfera, "engine": e, "m": m, "p": p, "kraj": kraj,
        # Wagi CKE (README, „Wagi CKE”): prognoza wyniku matury i pewniaki
        "prognoza": m.Prognoza(teraz=teraz), "pewniaki": p.Pewniaki(),
    }


def na_tle_kraju(m, p, proby: list[dict], fit) -> dict:
    """Łatwość krajowa (m.Latwosc) w raporcie, tylko odczyt. Każda próba dostaje x["lat"] (łatwość w %,
    None bez danych) i x["os_lat"]: łatwość „równoważna” na osi modelu (dla wykresów, gdy Profil liczy
    trudność z łatwości). Podsumowanie z ostatniej próby każdego zadania: działy (punkty vs przeciętny
    maturzysta), zadania trudne dla kraju (< 30%) i pełne arkusze z wynikiem kraju (zestawy_cke)."""
    lat_ok = hasattr(m, "Latwosc")
    ref = m._logit(m.PROFIL_LAT_REF) if lat_ok and hasattr(m, "PROFIL_LAT_REF") else None
    d_ref = m._logit(m.LAT_REF) - ref if ref is not None else None
    for x in proby:
        l = m.Latwosc(x["id"]) if lat_ok else None
        x["lat"] = l["latwosc"] if l else None
        x["lat_szac"] = bool(l and l["zrodlo"] == "szacunek")
        x["os_lat"] = None
        if fit is not None and fit.get("lat") and ref is not None:
            u = -(l["trudnosc"] - d_ref) if l else -(x["norma"] - m.SREDNIA_NORMA) * 0.8
            x["os_lat"] = 100 / (1 + math.exp(-(u + ref)))
    ost = {}
    for x in proby:
        ost[x["id"]] = x
    dz, trudne, n, szac = {}, [0.0, 0, 0], 0, 0
    for x in ost.values():
        if x["lat"] is None:
            continue
        pk, mx = (x["pkt"], x["maks"]) if x["maks"] else (None, p.Punkty(x["id"]))
        if not mx:
            continue
        pk = pk if pk is not None else x["sc"] / 10 * mx
        a = dz.setdefault(x["dzial"], [0.0, 0.0, 0, 0])
        a[0] += pk; a[1] += mx * x["lat"] / 100; a[2] += 1; a[3] += mx
        n += 1
        szac += x["lat_szac"]
        if x["lat"] < 30:
            trudne[0] += pk; trudne[1] += mx; trudne[2] += 1
    dzialy = sorted(({"dzial": k, "pkt": v[0], "oczek": v[1], "zadania": v[2], "maks": v[3]} for k, v in dz.items()),
                    key=lambda r: r["pkt"] - r["oczek"])
    cke = p.ZestawyCKE() if hasattr(p, "ZestawyCKE") else {}
    return {"zadania": n, "szacunek": szac, "dzialy": dzialy,
            "trudne": {"pkt": trudne[0], "maks": trudne[1], "zadania": trudne[2]}, "zestawy_cke": cke,
            "model_lat": bool(fit is not None and fit.get("lat"))}


# ============================================================================ wykresy

def _styl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.family": "STIXGeneral", "mathtext.fontset": "stix", "font.size": 7.5, "axes.labelsize": 7.5, "xtick.labelsize": 7, "ytick.labelsize": 7, "axes.titlesize": 7.5, "axes.titleweight": "normal",
        "axes.titlelocation": "left", "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.linewidth": 0.6,
        "xtick.color": INK2, "ytick.color": INK2, "xtick.major.width": 0.5, "ytick.major.width": 0.5,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.5, "axes.spines.top": False,
        "axes.spines.right": False, "figure.facecolor": "white", "axes.facecolor": "white", "legend.frameon": False,
        "legend.fontsize": 6.3, "savefig.bbox": None, "figure.constrained_layout.use": True,
        "figure.constrained_layout.h_pad": 0.03, "figure.constrained_layout.w_pad": 0.03, "pdf.fonttype": 42,
    })
    return plt


def _skrot(d: str, n: int = 22) -> str:
    zam = {"Wykładnicza i logarytmiczna": "Wykł. i logarytm.", "Wielomiany i wymierne": "Wielomiany i wymierne",
           "Równania i nierówności": "Równania i nierówn.", "Analiza i stochastyka": "Analiza i stochast.",
           "Wyrażenia algebraiczne": "Wyrażenia algebr.", "Geometria analityczna": "Geometria analit."}
    d = zam.get(d, d)
    return d if len(d) <= n else d[: n - 1] + "…"


def wykresy(D: dict, kat: Path) -> dict:
    import numpy as np
    plt = _styl()
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.patches import Patch
    kat.mkdir(parents=True, exist_ok=True)
    out = {}
    div = LinearSegmentedColormap.from_list("div", [CZERWONY, "#f0efec", NIEBIESKI])
    seq = LinearSegmentedColormap.from_list("seq", ["#f4f3ef", "#cde2fb", "#6da7ec", "#256abf", "#0d366b"])
    proby, dzis = D["proby"], D["dzis"]

    def zapisz(fig, nazwa):
        # styl artykułu: tytuł ryciny jest w \caption, więc usuwamy tytuły osi i pogrubione napisy-tytuły
        for ax in fig.axes:
            ax.set_title("")
        for tx in list(fig.texts):
            if tx.get_fontweight() in ("bold", 700):
                tx.remove()
        fig.savefig(kat / f"{nazwa}.pdf")
        plt.close(fig)
        out[nazwa] = f"wykresy/{nazwa}.pdf"

    # --- wykresy 2D w stylu publikacji: paleta Okabe-Ito (bezpieczna dla daltonistów), cienkie osie,
    # znaczniki z przedziałami zamiast grubych słupków, liczby przy danych zamiast legend tam, gdzie się da
    OI = {"nieb": "#0072B2", "pom": "#E69F00", "ziel": "#009E73", "czer": "#D55E00", "jasn": "#56B4E9",
          "fiol": "#CC79A7", "szary": "#7A7A7A", "ciem": "#222222"}
    ST2 = {"mocne": OI["ziel"], "niepewne": OI["szary"], "slabe": OI["czer"]}

    def osie(ax, siatka="y"):
        ax.grid(False)
        if siatka:
            ax.grid(axis=siatka, color="#E6E6E6", lw=.5, zorder=0)
        ax.set_axisbelow(True)
        for s_ in ("left", "bottom"):
            ax.spines[s_].set_color("#333333")
            ax.spines[s_].set_linewidth(.7)
        ax.tick_params(colors="#333333", direction="out", length=2.5, width=.6)

    # 1. trend: średnia dnia (wielkość = liczba prób), średnia krocząca 7 dni z pasmem ±1 SE, progi faz
    if proby:
        dni = defaultdict(list)
        for x in proby:
            dni[_dt(x["ts"]).date()].append(x["sc"] * 10)
        start = min(dni)
        os_d = [start + timedelta(days=i) for i in range((dzis - start).days + 1)]
        krocz, se = [], []
        for d0 in os_d:
            okno = [v for d1, vs in dni.items() if 0 <= (d0 - d1).days < 7 for v in vs]
            krocz.append(np.mean(okno) if okno else np.nan)
            se.append(np.std(okno, ddof=1) / math.sqrt(len(okno)) if len(okno) > 1 else np.nan)
        krocz, se = np.array(krocz), np.array(se)
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.1, 2.7), sharex=True, gridspec_kw={"height_ratios": [2.6, 1]})
        dd = sorted(dni)
        a1.fill_between(os_d, np.clip(krocz - se, 0, 100), np.clip(krocz + se, 0, 100), color=OI["nieb"], alpha=.15,
                        lw=0, label="±1 SE", zorder=2)
        a1.plot(os_d, krocz, color=OI["nieb"], lw=1.6, label="średnia krocząca (7 dni)", zorder=4)
        a1.scatter(dd, [np.mean(dni[d]) for d in dd], s=[10 + 5 * len(dni[d]) for d in dd], facecolor="white",
                   edgecolor=OI["ciem"], linewidth=.7, label="średnia dnia", zorder=5)
        for prog in (60, 75, 90):
            a1.axhline(prog, color=OI["szary"], lw=.6, ls=(0, (4, 3)), zorder=1)
            a1.annotate(f"{prog}%", (1, prog), xycoords=("axes fraction", "data"), xytext=(3, 0),
                        textcoords="offset points", va="center", fontsize=7, color=OI["szary"])
        a1.set_ylim(0, 100)
        a1.set_yticks([0, 25, 50, 75, 100])
        a1.set_ylabel("wynik zadania (%)")
        a1.legend(loc="lower left", ncol=3, handlelength=1.6, columnspacing=1.2)
        osie(a1)
        tyg = Counter((_dt(x["ts"]).date() - timedelta(days=_dt(x["ts"]).weekday())) for x in proby)
        wk = sorted(tyg)
        sr_ = [k_ + timedelta(days=3.5) for k_ in wk]
        a2.vlines(sr_, 0, [tyg[k_] for k_ in wk], color="#CFCFCF", lw=1, zorder=2)
        a2.scatter(sr_, [tyg[k_] for k_ in wk], s=16, color=OI["szary"], zorder=3)
        a2.set_ylim(0, max(tyg.values()) * 1.25)
        a2.set_ylabel("próby / tydz.")
        osie(a2)
        import matplotlib.dates as mdates
        a2.set_xlim(start - timedelta(days=1), dzis + timedelta(days=1))
        a2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        zapisz(fig, "trend")

    # 2. mapa aktywności: 8 tygodni x 7 dni, kwadratowe komórki ze skalą
    pon = dzis - timedelta(days=dzis.weekday())
    tyg8 = [pon - timedelta(weeks=k) for k in range(7, -1, -1)]
    mac = np.zeros((7, 8))
    for x in proby:
        d = _dt(x["ts"]).date()
        k = (d - timedelta(days=d.weekday()) - tyg8[0]).days // 7
        if 0 <= k < 8:
            mac[d.weekday(), k] += 1
    fig, ax = plt.subplots(figsize=(3.45, 2.2))
    vmax = max(4, mac.max())
    JJ, II = np.meshgrid(range(8), range(7))
    ax.scatter(JJ.ravel(), II.ravel(), s=5, color="#DDDDDD", lw=0, zorder=1)
    mk = mac > 0
    ax.scatter(JJ[mk], II[mk], s=25 + 130 * mac[mk] / vmax, color=OI["nieb"], alpha=.9, lw=0, zorder=3)
    for i_, j_ in zip(*np.nonzero(mk)):
        ax.text(j_, i_, int(mac[i_, j_]), ha="center", va="center", fontsize=6, color="white", zorder=4)
    ax.set_yticks(range(7), DNI_PL)
    ax.set_xticks(range(8), [d.strftime("%d.%m") for d in tyg8], fontsize=6.5)
    ax.set_xlim(-.6, 7.6)
    ax.set_ylim(6.6, -.6)
    ax.set_xlabel("tydzień od poniedziałku (liczba prób w dniu)")
    ax.grid(False)
    for s_ in ax.spines.values():
        s_.set_visible(False)
    ax.tick_params(length=0, colors="#333333")
    zapisz(fig, "aktywnosc")

    # 3. profil działów: jasny pasek 80% przedziału + kropka w kolorze opanowania (skala niebieska), progi jako delikatne linie
    prof = D["profil"]
    if prof.get("status") == "ok" and prof["dzialy"]:
        from matplotlib.colors import LinearSegmentedColormap as _LSC
        dz = sorted(prof["dzialy"], key=lambda x: x["opanowanie"])
        skala = _LSC.from_list("op", ["#E8A87C", "#9DB8D6", OI["nieb"]])
        fig, ax = plt.subplots(figsize=(3.6, 0.22 * len(dz) + 0.75))
        for prog in (60, 75, 90):
            ax.axvline(prog, color="#E3E3E3", lw=.8, zorder=1)
            ax.annotate(f"{prog}%", (prog, 1), xycoords=("data", "axes fraction"), xytext=(0, 2),
                        textcoords="offset points", ha="center", va="bottom", fontsize=6, color=OI["szary"])
        for i_, x in enumerate(dz):
            lo, hi = x["przedzial"]
            kol = skala(x["opanowanie"] / 100)
            ax.hlines(i_, lo, hi, color=kol, lw=3.2, alpha=.35, capstyle="round", zorder=2)
            ax.scatter([x["opanowanie"]], [i_], s=26, color=kol, edgecolor="white", linewidth=.7, zorder=4)
            pop = D["zmiana"].get(x["dzial"])
            dl = "" if pop is None or pop == x["opanowanie"] else f" ({x['opanowanie'] - pop:+d})"
            ax.annotate(f"{x['opanowanie']}%{dl}", (1, i_), xycoords=("axes fraction", "data"), xytext=(4, 0),
                        textcoords="offset points", va="center", fontsize=6.5, color=OI["ciem"])
        ax.set_yticks(range(len(dz)), [f"{_skrot(x['dzial'])} ({x['proby']})" for x in dz])
        ax.set_xlim(0, 100)
        ax.set_xticks([0, 25, 50, 75, 100])
        ax.set_xlabel("opanowanie (%); jasny pasek = 80% przedział")
        ax.set_ylim(-.6, len(dz) - .4)
        osie(ax, None)
        ax.spines["left"].set_visible(False)
        ax.tick_params(axis="y", length=0)
        zapisz(fig, "profil")

    # 4. wynik a trudność: próby jako kropki, krzywa modelu z 80% pasmem niepewności (metoda delta)
    if proby and D["fit"] is not None:
        b = D["fit"]["beta"]
        cov = np.asarray(D["fit"]["cov"])
        rng = np.random.default_rng(1)
        fig, ax = plt.subplots(figsize=(3.4, 2.4))
        y = np.array([x["sc"] * 10 for x in proby])
        if D["kraj"]["model_lat"]:
            # oś: łatwość krajowa (trudniej w prawo); kolumna trudności modelu = logit(łatwość) - logit(ref)
            n = np.array([x["os_lat"] for x in proby])
            xs = np.linspace(95, 3, 120)
            ref = D["m"].PROFIL_LAT_REF
            u = np.log(xs / (100 - xs)) - math.log(ref / (100 - ref))
        else:
            n = np.array([x["norma"] for x in proby])
            xs = np.linspace(min(1.5, n.min()), max(6, n.max()), 120)
            u = -(xs - 3.5)
        logit = b[0] + b[1] * u
        var = cov[0, 0] + u ** 2 * cov[1, 1] + 2 * u * cov[0, 1]
        sd = np.sqrt(np.maximum(var, 0))
        f = lambda z: 100 / (1 + np.exp(-z))
        ax.fill_between(xs, f(logit - 1.2816 * sd), f(logit + 1.2816 * sd), color=OI["nieb"], alpha=.14, lw=0,
                        label="80% przedział modelu", zorder=1)
        ax.plot(xs, f(logit), color=OI["nieb"], lw=1.5, label="model: szansa na komplet", zorder=3)
        ax.scatter(n + rng.normal(0, .6 if D["kraj"]["model_lat"] else .03, len(n)), np.clip(y + rng.normal(0, .8, len(y)), 0, 100), s=12,
                   facecolor="white", edgecolor=OI["ciem"], linewidth=.6, alpha=.9, label="próba", zorder=4)
        if D["kraj"]["model_lat"]:
            ax.set_xlim(97, 0)
            ax.set_xlabel("łatwość krajowa zadania, % (trudniej →)")
        else:
            ax.set_xlabel("trudność zadania (norma wektora)")
        ax.set_ylabel("wynik zadania (%)")
        ax.set_ylim(-3, 103)
        ax.set_yticks([0, 25, 50, 75, 100])
        osie(ax)
        fig.legend(loc="outside lower center", ncol=3, fontsize=6.3, handlelength=1.4, columnspacing=1)
        zapisz(fig, "trudnosc")

    # 5. tydzień w działach: każda próba to kółko (wynik %), romb to średnia działu, n przy wierszu
    tyg = D["tyg"]
    if tyg:
        c = defaultdict(list)
        for x in tyg:
            c[x["dzial"]].append(x["sc"] * 10)
        dz = sorted(c, key=lambda d: np.mean(c[d]))
        rng = np.random.default_rng(3)
        fig, ax = plt.subplots(figsize=(3.45, 0.24 * len(dz) + 0.85))
        for prog in (60, 75, 90):
            ax.axvline(prog, color="#BDBDBD", lw=.6, ls=(0, (4, 3)), zorder=1)
        for i_, d in enumerate(dz):
            v = np.array(c[d])
            ax.hlines(i_, 0, v.mean(), color="#DADADA", lw=.8, zorder=1)
            ax.scatter(v, i_ + rng.uniform(-.12, .12, len(v)), s=14, facecolor="white", edgecolor=OI["szary"],
                       linewidth=.7, zorder=3)
            ax.scatter([v.mean()], [i_], s=30, marker="D", color=OI["nieb"], zorder=4)
            ax.annotate(f"{v.mean():.0f}%  (n={len(v)})", (1, i_), xycoords=("axes fraction", "data"), xytext=(3, 0),
                        textcoords="offset points", va="center", fontsize=6.3, color=OI["ciem"])
        ax.set_yticks(range(len(dz)), [_skrot(d) for d in dz])
        ax.set_ylim(-.6, len(dz) - .4)
        ax.set_xlim(-3, 103)
        ax.set_xticks([0, 25, 50, 75, 100])
        ax.set_xlabel("wynik zadania w tym tygodniu (%)")
        osie(ax, "x")
        from matplotlib.lines import Line2D
        fig.legend(handles=[Line2D([], [], marker="o", ls="", mfc="white", mec=OI["szary"], ms=4, label="jedna próba"),
                            Line2D([], [], marker="D", ls="", color=OI["nieb"], ms=4, label="średnia działu")],
                   loc="outside lower center", ncol=2, fontsize=6.5)
        zapisz(fig, "tydzien_dzialy")

    # 6. typy błędów: wykres lizakowy; dwie serie (od początku / ten tydzień) tylko gdy się różnią, przesunięte w pionie
    ty_t = Counter(x["typ"] for x in tyg if x["typ"])
    ty_c = Counter(x["typ"] for x in proby if x["typ"])
    if ty_c:
        ty = sorted([t_ for t_ in TYPY if ty_c[t_]], key=lambda t_: (ty_c[t_], ty_t[t_]))
        dwie = ty_t != ty_c and sum(ty_t.values()) > 0
        fig, ax = plt.subplots(figsize=(3.6, 0.26 * len(ty) + 0.95))
        yy = np.arange(len(ty))
        d_ = .16 if dwie else 0
        ax.hlines(yy + d_, 0, [ty_c[t_] for t_ in ty], color="#CFCFCF", lw=1, zorder=1)
        ax.scatter([ty_c[t_] for t_ in ty], yy + d_, s=26, color=OI["nieb"], zorder=3,
                   label="od początku" if dwie else f"wszystkie błędy (n={sum(ty_c.values())})")
        for i_, t_ in enumerate(ty):
            ax.annotate(str(ty_c[t_]), (ty_c[t_], i_ + d_), xytext=(5, 0), textcoords="offset points", va="center",
                        fontsize=6.5, color=OI["nieb"])
        if dwie:
            ax.hlines(yy - d_, 0, [ty_t[t_] for t_ in ty], color="#F1DDB0", lw=1, zorder=1)
            ax.scatter([ty_t[t_] for t_ in ty], yy - d_, s=22, marker="D", color=OI["pom"], zorder=4, label="ten tydzień")
            for i_, t_ in enumerate(ty):
                if ty_t[t_]:
                    ax.annotate(str(ty_t[t_]), (ty_t[t_], i_ - d_), xytext=(5, 0), textcoords="offset points",
                                va="center", fontsize=6.5, color=OI["pom"])
        ax.set_yticks(yy, ty)
        ax.set_ylim(-.6, len(ty) - .4)
        ax.set_xlim(0, max(ty_c.values()) * 1.2 + .3)
        ax.xaxis.get_major_locator().set_params(integer=True)
        ax.set_xlabel("liczba błędów")
        osie(ax, "x")
        fig.legend(loc="outside lower center", ncol=2, fontsize=6.5)
        zapisz(fig, "bledy")

    # 7. harmonogram (Gantt) z progami i pełnymi arkuszami
    import matplotlib.dates as mdates
    h = D["harm"]
    fazy = []
    for t in h["tygodnie"]:
        if fazy and fazy[-1]["faza"] == t["faza"]:
            fazy[-1]["do"] = t["do"]
        else:
            fazy.append({"faza": t["faza"], "od": t["od"], "do": t["do"]})
    kol = {f: KATEG[i % 8] for i, f in enumerate(dict.fromkeys(x["faza"] for x in fazy))}
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.1, 2.45), sharex=True, gridspec_kw={"height_ratios": [.8, 2.2]})
    for f in fazy:
        d0, d1 = date.fromisoformat(f["od"]), date.fromisoformat(f["do"]) + timedelta(days=1)
        a1.barh(0, (d1 - d0).days, left=d0, color=kol[f["faza"]], edgecolor="white", linewidth=1.5, height=.8, alpha=.85)
        et = re.sub(r"^\d\s*", "", f["faza"]).replace("Nowe i zapomniane działy", "Nowe i zapomniane")
        et = et.replace("Trudne typy zadań otwartych", "Trudne zadania otwarte").replace("Przerwa świąteczna", "Święta")
        a1.text(d0 + (d1 - d0) / 2, 0, et, ha="center", va="center", fontsize=5.8 if (d1 - d0).days > 20 else 5,
                color=INK_raport, fontweight="bold")
    a1.set_yticks([])
    a1.grid(False)
    a1.set_title("Plan do matury: fazy, progi i pełne arkusze")
    for a in (a1, a2):
        a.axvline(dzis, color=INK_raport, lw=1)
    cele = sorted((date.fromisoformat(k), v) for k, v in h["cele"].items())
    xs, ys = [], []
    for d, v in cele:
        pr = next((n for kk, n in {"60%": 60, "75%": 75, "90%": 90}.items() if kk in v), None)
        if pr:
            xs.append(d)
            ys.append(pr)
    if xs:
        a2.step([date.fromisoformat(h["tygodnie"][0]["od"])] + xs, [ys[0]] + ys, where="pre", color=MUTED, lw=1,
                ls=(0, (3, 2)), label="próg fazy")
        a2.scatter(xs, ys, marker="D", s=16, color=MUTED, zorder=3)
    pel = [x for x in D["pelne"] if x["proc"] is not None]
    if pel:
        a2.plot([date.fromisoformat(x["data"]) for x in pel], [x["proc"] for x in pel], color=NIEBIESKI, lw=2,
                marker="o", ms=5, mec="white", label="pełny arkusz, %", zorder=4)
        for x in pel:
            a2.annotate(f"{x['proc']}%", (date.fromisoformat(x["data"]), x["proc"]), textcoords="offset points",
                        xytext=(0, -11), ha="center", fontsize=6, color=INK_raport)
    plan_p = [date.fromisoformat(t["od"]) + timedelta(days=1) for t in h["tygodnie"] if t.get("pelny")]
    a2.scatter(plan_p, [3] * len(plan_p), marker="|", s=50, color=NIEBIESKI, label="planowany pełny arkusz")
    m_dt = datetime.fromisoformat(h["matura"]).date()
    a2.axvline(m_dt, color=CZERWONY, lw=1)
    a2.text(m_dt, 50, "matura ", ha="right", fontsize=6, color=CZERWONY, rotation=90, va="center")
    a2.text(dzis, 50, "dziś ", ha="right", fontsize=6, color=INK_raport, rotation=90, va="center")
    a2.set_ylim(0, 100)
    a2.set_ylabel("wynik arkusza, %")
    fig.legend(loc="outside lower center", ncol=3, fontsize=6)
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    zapisz(fig, "harmonogram")

    # 8. 3D: szansa na komplet w zależności od działu i trudności (model Profil)
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

    def panele(ax):
        for a in (ax.xaxis, ax.yaxis, ax.zaxis):
            a.pane.set_facecolor("#f7f7f5")
            a.pane.set_edgecolor(GRID)
            a._axinfo["grid"]["color"] = GRID
            a._axinfo["grid"]["linewidth"] = .4

    fit = D["fit"]
    if fit is not None and prof.get("status") == "ok":
        b = fit["beta"]
        dz = sorted(prof["dzialy"], key=lambda x: x["opanowanie"])
        lat = D["kraj"]["model_lat"]
        if lat:
            # oś trudności jako „100 - łatwość krajowa”, żeby trudniej było dalej jak dawniej
            trud = np.linspace(20.0, 95.0, 33)
            ref = D["m"].PROFIL_LAT_REF
            ucol = np.log((100 - trud) / trud) - math.log(ref / (100 - ref))
        else:
            trud = np.linspace(2.0, 6.0, 33)
            ucol = -(trud - 3.5)
        TT, II = np.meshgrid(trud, np.arange(len(dz)))
        ZZ = np.zeros_like(TT)
        for i, x in enumerate(dz):
            k = fit["dzialy"].index(x["dzial"])
            ZZ[i] = 1 / (1 + np.exp(-(b[0] + b[2 + k] + b[1] * ucol)))
        fig = plt.figure(figsize=(7.1, 3.3), layout=None)
        ax = fig.add_axes([0.0, -0.1, 0.96, 1.2], projection="3d", computed_zorder=False)
        ax.plot_surface(TT, II, 100 * ZZ, facecolors=div(ZZ), rstride=1, cstride=1, linewidth=0, antialiased=True,
                        shade=False, alpha=.95, zorder=1)
        for i in range(len(dz)):
            ax.plot(trud, [i] * len(trud), 100 * ZZ[i], color="white", lw=.7, zorder=3)
        for t50 in ((40.0, 60.0, 80.0) if lat else (3.0, 4.0, 5.0)):
            j = int(np.argmin(np.abs(trud - t50)))
            ax.plot([t50] * len(dz), range(len(dz)), 100 * ZZ[:, j], color=INK2, lw=.5, ls=(0, (2, 2)), zorder=3)
        ax.set_yticks(range(len(dz)), [_skrot(x["dzial"], 22) for x in dz], fontsize=5.3, ha="left", va="center")
        if lat:
            ax.set_xticks([20, 40, 60, 80, 95], ["80", "60", "40", "20", "5"], fontsize=5.5)
        else:
            ax.set_xticks([2, 3, 4, 5, 6], ["2", "3", "4", "5", "6"], fontsize=5.5)
        ax.set_zlim(0, 100)
        ax.set_zticks([0, 25, 50, 75, 100], ["0", "25", "50", "75", "100%"], fontsize=5.5)
        ax.set_xlabel("łatwość krajowa zadania, %" if lat else "trudność zadania (norma)", fontsize=6, labelpad=-6)
        ax.tick_params(axis="x", pad=-4)
        ax.tick_params(axis="y", pad=-3)
        ax.tick_params(axis="z", pad=-1)
        ax.view_init(elev=30, azim=-42)
        ax.set_box_aspect((1.5, 2.4, .8), zoom=1.05)
        cax = fig.add_axes([0.93, 0.25, 0.013, 0.5])
        cb = fig.colorbar(plt.cm.ScalarMappable(cmap=div, norm=plt.Normalize(0, 100)), cax=cax)
        cb.set_label("szansa na komplet, %", fontsize=6)
        cb.ax.tick_params(labelsize=5.5)
        cb.outline.set_visible(False)
        panele(ax)
        fig.text(.01, .965, "Krajobraz szans: dział × trudność → szansa na komplet punktów", fontsize=7.5,
                 fontweight="bold", color=INK_raport)
        zapisz(fig, "krajobraz3d")

    # 9. 3D: sfera kompetencji (harmoniki sferyczne, jak Opanowanie), dwa widoki
    sf = D["sfera"]
    if sf is not None:
        m = D["m"]
        th, ph = np.meshgrid(np.linspace(0, 2 * np.pi, 140), np.linspace(0, np.pi, 70))
        r = np.zeros_like(th)
        for (l_deg, mm), c in zip(sf["terms"], sf["coeffs"]):
            r += c * m.real_sph_harm(mm, l_deg, th, ph)
        r = np.clip(r, 0, 10)
        X, Y, Z = np.sin(ph) * np.cos(th), np.sin(ph) * np.sin(th), np.cos(ph)
        fig = plt.figure(figsize=(7.1, 2.75), layout=None)
        for k, azim in enumerate((-60, 120)):
            ax = fig.add_axes([0.0 + 0.44 * k, 0.0, 0.46, 0.92], projection="3d", computed_zorder=False)
            ax.plot_surface(X, Y, Z, facecolors=div(r / 10), rstride=1, cstride=1, linewidth=0, antialiased=False,
                            shade=False, zorder=1)
            ax.view_init(elev=18, azim=azim)
            el, az = np.radians(18), np.radians(azim)
            v = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
            for u, sc in sf["pkt"]:
                if float(np.dot(u, v)) > 0.05:
                    ax.scatter(*(1.02 * u), s=9, color=div(sc / 10), edgecolor=INK_raport, linewidth=.45, zorder=5)
            for i, cp in enumerate(sf["kryt"]):
                u = np.array(cp["kierunek_3d"])
                if float(np.dot(u, v)) > 0:
                    ax.scatter(*(1.06 * u), s=46, marker="v" if cp["typ"] == "MINIMUM" else "^", color="white",
                               edgecolor=INK_raport, linewidth=.9, zorder=6)
                    ax.text(*(1.2 * u), f"{i + 1}", fontsize=7, fontweight="bold", color=INK_raport, zorder=7)
            ax.set_xlim(-.8, .8)
            ax.set_ylim(-.8, .8)
            ax.set_zlim(-.8, .8)
            ax.set_box_aspect((1, 1, 1))
            ax.set_axis_off()
            ax.text2D(.5, .02, ["widok przedni", "widok tylny (obrót o 180°)"][k], transform=ax.transAxes,
                      ha="center", fontsize=6, color=INK2)
        cax = fig.add_axes([0.915, 0.18, 0.015, 0.6])
        sm = plt.cm.ScalarMappable(cmap=div, norm=plt.Normalize(0, 100))
        cb = fig.colorbar(sm, cax=cax)
        cb.set_label("przewidywany wynik, %", fontsize=6)
        cb.ax.tick_params(labelsize=5.5)
        cb.outline.set_visible(False)
        fig.text(.01, .95, "Sfera kompetencji: rzut $\\mathbb{R}^{160}\\to S^2$, harmoniki sferyczne", fontsize=7.5,
                 fontweight="bold", color=INK_raport)
        zapisz(fig, "sfera3d")

    # 10. 3D: przestrzeń zadań bazy (PCA 160 → 3) i zrobione zadania
    e = D["engine"]
    P = (e.vectors - e.pca_mean) @ e.pca_basis
    ok_mask = e.norms > 0.5
    fig = plt.figure(figsize=(3.45, 3.0), layout=None)
    ax = fig.add_axes([-0.08, -0.06, 1.12, 1.06], projection="3d", computed_zorder=False)
    rng = np.random.default_rng(0)
    sel = rng.choice(np.where(ok_mask)[0], size=min(2500, int(ok_mask.sum())), replace=False)
    lo, hi = np.percentile(P[ok_mask], 1.5, axis=0), np.percentile(P[ok_mask], 98.5, axis=0)
    P = np.clip(P, lo, hi)
    ax.set_xlim(lo[0], hi[0])
    ax.set_ylim(lo[1], hi[1])
    ax.set_zlim(lo[2], hi[2])
    ax.scatter(P[sel, 0], P[sel, 1], P[sel, 2], s=2.2, color="#a9a8a1", alpha=.45, linewidth=0, zorder=1)
    if proby:
        ost = {}
        for x in proby:
            ost[x["id"]] = x
        idx = [e.id_to_idx[i] for i in ost]
        ax.scatter(P[idx, 0], P[idx, 1], P[idx, 2], s=13, c=[div(x["sc"] / 10) for x in ost.values()],
                   edgecolor=INK_raport, linewidth=.35, zorder=5)
    panele(ax)
    for a in (ax.xaxis, ax.yaxis, ax.zaxis):
        a.set_ticklabels([])
    ax.set_xlabel("PC1", fontsize=6, labelpad=-14)
    ax.set_ylabel("PC2", fontsize=6, labelpad=-14)
    ax.set_zlabel("PC3", fontsize=6, labelpad=-14)
    ax.view_init(elev=20, azim=-50)
    fig.text(.02, .95, "Przestrzeń 4608 zadań (PCA)", fontsize=7.5, fontweight="bold", color=INK_raport)
    zapisz(fig, "przestrzen3d")

    # 10a. ewolucja opanowania: dział x tydzień (3D, słupki) z profilem liczonym na kolejne granice tygodni
    if proby and len({_dt(x["ts"]).date() for x in proby}) >= 3:
        mm = D["m"]
        T = D["teraz"]
        liczba = min(8, max(2, int((T - proby[0]["ts"]) // (7 * 86400)) + 1))
        kol_t, wart = [], {}
        for w in range(liczba):
            tw = T - (liczba - 1 - w) * 7 * 86400
            sub = [(x["id"], x["sc"] / 10, x["ts"]) for x in proby if x["ts"] <= tw]
            if len(sub) < 3:
                continue
            try:
                pr = mm.Profil(proby=sub, teraz=tw)
            except Exception:
                continue
            if pr.get("status") != "ok":
                continue
            kol_t.append(_dt(tw).date())
            for x in pr["dzialy"]:
                wart.setdefault(x["dzial"], {})[len(kol_t) - 1] = x["opanowanie"]
        if len(kol_t) >= 2 and wart:
            dz = sorted(wart, key=lambda d: wart[d].get(len(kol_t) - 1, 0))
            fig = plt.figure(figsize=(7.1, 3.1), layout=None)
            ax = fig.add_axes([0.0, -0.08, 0.96, 1.15], projection="3d", computed_zorder=False)
            xs, ys, zs, cs = [], [], [], []
            for i, d in enumerate(dz):
                for j in range(len(kol_t)):
                    v = wart[d].get(j)
                    if v is None:
                        continue
                    xs.append(j); ys.append(i); zs.append(v); cs.append(div(v / 100))
            ax.bar3d(np.array(xs) - .35, np.array(ys) - .35, np.zeros(len(zs)), .7, .7, zs, color=cs, shade=True,
                     edgecolor="white", linewidth=.2)
            ax.set_xticks(range(len(kol_t)), [f"{d:%d.%m}" for d in kol_t], fontsize=5.3)
            ax.set_yticks(range(len(dz)), [_skrot(d, 20) for d in dz], fontsize=5.0, ha="left", va="center")
            ax.set_zlim(0, 100)
            ax.set_zticks([0, 25, 50, 75, 100], ["0", "25", "50", "75", "100%"], fontsize=5.5)
            ax.set_xlabel("koniec tygodnia", fontsize=6, labelpad=-4)
            ax.tick_params(axis="x", pad=-4); ax.tick_params(axis="y", pad=-3); ax.tick_params(axis="z", pad=-1)
            ax.view_init(elev=26, azim=-58)
            ax.set_box_aspect((1.8, 2.2, .8), zoom=1.05)
            panele(ax)
            fig.text(.01, .965, "Ewolucja opanowania: dział × tydzień → opanowanie (model)", fontsize=7.5,
                     fontweight="bold", color=INK_raport)
            zapisz(fig, "ewolucja3d")

    # 10b. rozrzut 3D: czas x trudność x wynik zadania, z płaszczyzną trendu
    if len(proby) >= 5:
        t0 = proby[0]["ts"]
        X = np.array([(x["ts"] - t0) / 86400 for x in proby])
        Y = np.array([x["norma"] for x in proby])
        Z = np.array([x["sc"] * 10 for x in proby])
        fig = plt.figure(figsize=(3.45, 3.0), layout=None)
        ax = fig.add_axes([-0.08, -0.06, 1.12, 1.06], projection="3d", computed_zorder=False)
        ax.scatter(X, Y, Z, c=[div(z / 100) for z in Z], s=14, edgecolor="white", linewidth=.3, depthshade=False)
        if np.ptp(X) > 0 and np.ptp(Y) > 0:
            A_ = np.c_[X, Y, np.ones_like(X)]
            co, *_ = np.linalg.lstsq(A_, Z, rcond=None)
            gx, gy = np.meshgrid(np.linspace(X.min(), X.max(), 8), np.linspace(Y.min(), Y.max(), 8))
            ax.plot_surface(gx, gy, np.clip(co[0] * gx + co[1] * gy + co[2], 0, 100), color=NIEBIESKI, alpha=.18,
                            linewidth=0, shade=False)
        ax.set_xlabel("dzień od 1. próby", fontsize=6, labelpad=-8)
        ax.set_ylabel("trudność (norma)", fontsize=6, labelpad=-8)
        ax.set_zlabel("wynik, %", fontsize=6, labelpad=-12)
        ax.tick_params(labelsize=5, pad=-4)
        ax.set_zlim(0, 100)
        ax.view_init(elev=22, azim=-55)
        panele(ax)
        fig.text(.02, .95, "Czas × trudność × wynik", fontsize=7.5, fontweight="bold", color=INK_raport)
        zapisz(fig, "czas_trudnosc3d")

    # 11. zmiana profilu: strzałka od stanu sprzed tygodnia do obecnego
    if prof.get("status") == "ok" and D["zmiana"]:
        dz = [x for x in prof["dzialy"] if x["dzial"] in D["zmiana"]]
        dz.sort(key=lambda x: x["opanowanie"] - D["zmiana"][x["dzial"]])
        if dz:
            fig, ax = plt.subplots(figsize=(3.45, 0.21 * len(dz) + 0.85))
            for i, x in enumerate(dz):
                a, b = D["zmiana"][x["dzial"]], x["opanowanie"]
                kol = OI["ziel"] if b > a else (OI["czer"] if b < a else OI["szary"])
                ax.scatter([a], [i], s=14, facecolor="white", edgecolor=OI["szary"], linewidth=.8, zorder=3)
                if b != a:
                    ax.annotate("", (b, i), (a, i), arrowprops=dict(arrowstyle="-|>", color=kol, lw=1.1,
                                                                    mutation_scale=7, shrinkA=2, shrinkB=0), zorder=2)
                ax.scatter([b], [i], s=16, color=kol, zorder=4)
                ax.annotate(f"{b - a:+d}", (1, i), xycoords=("axes fraction", "data"), xytext=(3, 0),
                            textcoords="offset points", va="center", fontsize=6.3, color=kol)
            ax.set_yticks(range(len(dz)), [_skrot(x["dzial"]) for x in dz])
            ax.set_xlim(0, 100)
            ax.set_xticks([0, 25, 50, 75, 100])
            ax.set_xlabel("opanowanie (%): ○ tydzień temu → ● dziś")
            osie(ax, "x")
            zapisz(fig, "zmiana_profilu")

    # 12. błędy w działach: macierz kropek (wiersz = dział, kolumna = typ, wielkość kropki = liczba błędów), suma w wierszu
    bl_all = [x for x in proby if x["typ"] and x["wynik"] != "ok"]
    if len(bl_all) >= 3:
        dz = sorted({x["dzial"] for x in bl_all}, key=lambda d: sum(x["dzial"] == d for x in bl_all))[-10:]
        ty = [t_ for t_ in TYPY if any(x["typ"] == t_ for x in bl_all)]
        M = np.array([[sum(1 for x in bl_all if x["dzial"] == d and x["typ"] == t_) for t_ in ty] for d in dz])
        fig, ax = plt.subplots(figsize=(3.45, 0.26 * len(dz) + 1.15))
        mx = max(1, M.max())
        for (i_, j_), v in np.ndenumerate(M):
            if v:
                ax.scatter(j_, i_, s=38 + 110 * v / mx, color=OI["nieb"], alpha=.9, lw=0, zorder=3)
                ax.text(j_, i_, str(v), ha="center", va="center", fontsize=5.8, color="white", zorder=4)
        for i_ in range(len(dz)):
            ax.annotate(f"{M[i_].sum()}", (1, i_), xycoords=("axes fraction", "data"), xytext=(4, 0),
                        textcoords="offset points", va="center", fontsize=6.5, color=OI["ciem"])
        ax.annotate("razem", (1, len(dz) - .4), xycoords=("axes fraction", "data"), xytext=(4, 2),
                    textcoords="offset points", va="bottom", fontsize=6, color=OI["szary"])
        ax.set_xticks(range(len(ty)), ty, rotation=35, ha="right")
        ax.set_yticks(range(len(dz)), [_skrot(d) for d in dz])
        ax.set_xlim(-.6, len(ty) - .4)
        ax.set_ylim(-.6, len(dz) - .4)
        ax.grid(True, color="#ECECEC", lw=.5, zorder=0)
        ax.set_axisbelow(True)
        for s_ in ax.spines.values():
            s_.set_visible(False)
        ax.tick_params(length=0, colors="#333333")
        zapisz(fig, "dzial_typ_bledu")

    # 13. zaległe powtórki: wykres lizakowy dni po terminie z progiem 7 dni
    if D["powt"]:
        pw = sorted(D["powt"], key=lambda x: x["zalegle_dni"])[-10:]
        fig, ax = plt.subplots(figsize=(3.45, 0.21 * len(pw) + 0.85))
        v = [x["zalegle_dni"] for x in pw]
        kol = [OI["czer"] if d > 7 else OI["pom"] for d in v]
        ax.hlines(range(len(pw)), 0, v, color=kol, lw=1.2, zorder=2)
        ax.scatter(v, range(len(pw)), s=18, color=kol, zorder=3)
        ax.axvline(7, color=OI["szary"], lw=.6, ls=(0, (4, 3)), zorder=1)
        ax.annotate("7 dni", (7, 1), xycoords=("data", "axes fraction"), xytext=(2, -2), textcoords="offset points",
                    va="top", fontsize=6.3, color=OI["szary"])
        ax.set_yticks(range(len(pw)), [f"{x['nazwa'][-20:]} ({x['pierwotne']})" for x in pw], fontsize=6.3)
        ax.set_xlim(0, max(v) * 1.08)
        ax.set_xlabel("dni po terminie powtórki")
        osie(ax, "x")
        zapisz(fig, "powtorki")
    return out
    return out


# ============================================================================ LaTeX

PREAMBULA = r"""\documentclass[10pt,a4paper]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage[polish]{babel}
\usepackage{mathptmx}
\DeclareUnicodeCharacter{2212}{\ensuremath{-}}\DeclareUnicodeCharacter{00D7}{\ensuremath{\times}}
\DeclareUnicodeCharacter{2192}{\ensuremath{\to}}\DeclareUnicodeCharacter{2248}{\ensuremath{\approx}}
\DeclareUnicodeCharacter{03C0}{\ensuremath{\pi}}\DeclareUnicodeCharacter{03B1}{\ensuremath{\alpha}}\DeclareUnicodeCharacter{03B2}{\ensuremath{\beta}}\DeclareUnicodeCharacter{03B3}{\ensuremath{\gamma}}\DeclareUnicodeCharacter{03B4}{\ensuremath{\delta}}\DeclareUnicodeCharacter{0394}{\ensuremath{\Delta}}\DeclareUnicodeCharacter{03B5}{\ensuremath{\varepsilon}}\DeclareUnicodeCharacter{03B8}{\ensuremath{\theta}}\DeclareUnicodeCharacter{03BB}{\ensuremath{\lambda}}\DeclareUnicodeCharacter{03BC}{\ensuremath{\mu}}\DeclareUnicodeCharacter{03C3}{\ensuremath{\sigma}}\DeclareUnicodeCharacter{03A3}{\ensuremath{\Sigma}}\DeclareUnicodeCharacter{03C6}{\ensuremath{\varphi}}\DeclareUnicodeCharacter{03C9}{\ensuremath{\omega}}\DeclareUnicodeCharacter{03A9}{\ensuremath{\Omega}}\DeclareUnicodeCharacter{2264}{\ensuremath{\leq}}\DeclareUnicodeCharacter{2265}{\ensuremath{\geq}}\DeclareUnicodeCharacter{2260}{\ensuremath{\neq}}\DeclareUnicodeCharacter{221A}{\ensuremath{\surd}}\DeclareUnicodeCharacter{221E}{\ensuremath{\infty}}\DeclareUnicodeCharacter{2208}{\ensuremath{\in}}\DeclareUnicodeCharacter{2209}{\ensuremath{\notin}}\DeclareUnicodeCharacter{2229}{\ensuremath{\cap}}\DeclareUnicodeCharacter{222A}{\ensuremath{\cup}}\DeclareUnicodeCharacter{2282}{\ensuremath{\subset}}\DeclareUnicodeCharacter{2286}{\ensuremath{\subseteq}}\DeclareUnicodeCharacter{2205}{\ensuremath{\emptyset}}\DeclareUnicodeCharacter{21D2}{\ensuremath{\Rightarrow}}\DeclareUnicodeCharacter{21D4}{\ensuremath{\Leftrightarrow}}\DeclareUnicodeCharacter{2261}{\ensuremath{\equiv}}\DeclareUnicodeCharacter{00B7}{\ensuremath{\cdot}}\DeclareUnicodeCharacter{22C5}{\ensuremath{\cdot}}\DeclareUnicodeCharacter{2032}{\ensuremath{\prime}}\DeclareUnicodeCharacter{2220}{\ensuremath{\angle}}\DeclareUnicodeCharacter{25B3}{\ensuremath{\triangle}}\DeclareUnicodeCharacter{2225}{\ensuremath{\parallel}}\DeclareUnicodeCharacter{22A5}{\ensuremath{\perp}}\DeclareUnicodeCharacter{2211}{\ensuremath{\sum}}\DeclareUnicodeCharacter{222B}{\ensuremath{\int}}\DeclareUnicodeCharacter{00B1}{\ensuremath{\pm}}\DeclareUnicodeCharacter{2213}{\ensuremath{\mp}}\DeclareUnicodeCharacter{2218}{\ensuremath{\circ}}\DeclareUnicodeCharacter{2115}{\ensuremath{\mathbb{N}}}\DeclareUnicodeCharacter{2124}{\ensuremath{\mathbb{Z}}}\DeclareUnicodeCharacter{211A}{\ensuremath{\mathbb{Q}}}\DeclareUnicodeCharacter{211D}{\ensuremath{\mathbb{R}}}\DeclareUnicodeCharacter{00B2}{\textsuperscript{2}}\DeclareUnicodeCharacter{00B3}{\ensuremath{^3}}\DeclareUnicodeCharacter{00B9}{\ensuremath{^1}}\DeclareUnicodeCharacter{2070}{\ensuremath{^0}}\DeclareUnicodeCharacter{2074}{\ensuremath{^4}}\DeclareUnicodeCharacter{2081}{\ensuremath{_1}}\DeclareUnicodeCharacter{2082}{\ensuremath{_2}}\DeclareUnicodeCharacter{2075}{\ensuremath{^5}}\DeclareUnicodeCharacter{2076}{\ensuremath{^6}}\DeclareUnicodeCharacter{2077}{\ensuremath{^7}}\DeclareUnicodeCharacter{2078}{\ensuremath{^8}}\DeclareUnicodeCharacter{2079}{\ensuremath{^9}}\DeclareUnicodeCharacter{207A}{\ensuremath{^+}}\DeclareUnicodeCharacter{207B}{\ensuremath{^-}}\DeclareUnicodeCharacter{207F}{\ensuremath{^n}}\DeclareUnicodeCharacter{2080}{\ensuremath{_0}}\DeclareUnicodeCharacter{2083}{\ensuremath{_3}}\DeclareUnicodeCharacter{2084}{\ensuremath{_4}}\DeclareUnicodeCharacter{2085}{\ensuremath{_5}}\DeclareUnicodeCharacter{2086}{\ensuremath{_6}}\DeclareUnicodeCharacter{2087}{\ensuremath{_7}}\DeclareUnicodeCharacter{2088}{\ensuremath{_8}}\DeclareUnicodeCharacter{2089}{\ensuremath{_9}}\DeclareUnicodeCharacter{208A}{\ensuremath{_+}}\DeclareUnicodeCharacter{208B}{\ensuremath{_-}}\DeclareUnicodeCharacter{2099}{\ensuremath{_n}}\DeclareUnicodeCharacter{2096}{\ensuremath{_k}}\DeclareUnicodeCharacter{2713}{\ensuremath{\checkmark}}\DeclareUnicodeCharacter{2227}{\ensuremath{\wedge}}\DeclareUnicodeCharacter{2228}{\ensuremath{\vee}}\DeclareUnicodeCharacter{2200}{\ensuremath{\forall}}\DeclareUnicodeCharacter{2203}{\ensuremath{\exists}}\DeclareUnicodeCharacter{2223}{\ensuremath{\mid}}\DeclareUnicodeCharacter{2245}{\ensuremath{\cong}}\DeclareUnicodeCharacter{223C}{\ensuremath{\sim}}\DeclareUnicodeCharacter{2194}{\ensuremath{\leftrightarrow}}\DeclareUnicodeCharacter{21D0}{\ensuremath{\Leftarrow}}
\usepackage[top=2.2cm,bottom=2.4cm,left=2.2cm,right=2.2cm]{geometry}
\usepackage{microtype,graphicx,booktabs,tabularx,xcolor,enumitem,amsmath,amssymb,bm,lastpage,fancyhdr,needspace}
\usepackage[font=small,labelfont=bf,labelsep=period,skip=4pt]{caption}
\usepackage{subcaption}
\usepackage[section]{placeins}
\usepackage[numbers,sort&compress]{natbib}
\usepackage[colorlinks,linkcolor=akcentc,citecolor=akcentc,urlcolor=akcentc]{hyperref}
\captionsetup[figure]{name=Rys.}\captionsetup[table]{name=Tab.}
\definecolor{ink2}{HTML}{52514E}\definecolor{muted}{HTML}{898781}\definecolor{akcentc}{HTML}{0D366B}
\definecolor{dobre}{HTML}{0CA30C}\definecolor{dobret}{HTML}{006300}\definecolor{uwaga}{HTML}{B07800}
\definecolor{zle}{HTML}{D03B3B}
\pagestyle{fancy}\fancyhf{}
\renewcommand{\headrulewidth}{0pt}
\fancyhead[C]{\footnotesize\itshape Modelowanie opanowania materiału maturalnego \textbullet{} @@NAGLOWEK@@}
\fancyfoot[C]{\small\thepage}
\newcommand{\brak}[1]{\begin{quote}\small\itshape Brak danych. #1\end{quote}}
\newcommand{\znak}[2]{\textcolor{#1}{\scriptsize\bfseries #2}}
\newcommand{\sigm}{\sigma}
\renewcommand{\arraystretch}{1.1}
\setlist{nosep,leftmargin=1.4em}
\setlength{\textfloatsep}{10pt plus 2pt}\setlength{\floatsep}{8pt plus 2pt}
\renewcommand{\topfraction}{.9}\renewcommand{\bottomfraction}{.6}\renewcommand{\textfraction}{.08}\renewcommand{\floatpagefraction}{.75}
\begin{document}
"""

BIBLIOGRAFIA = r"""\begin{thebibliography}{9}\small
\bibitem{hastie} T.~Hastie, R.~Tibshirani, J.~Friedman. \emph{The Elements of Statistical Learning}, wyd.~2. Springer, 2009.
\bibitem{rasch} G.~Rasch. \emph{Probabilistic Models for Some Intelligence and Attainment Tests}. Danish Institute for Educational Research, 1960.
\bibitem{corbett} A.~T.~Corbett, J.~R.~Anderson. Knowledge tracing: modeling the acquisition of procedural knowledge. \emph{User Modeling and User-Adapted Interaction}, 4:253--278, 1994.
\bibitem{mackay} D.~J.~C.~MacKay. The evidence framework applied to classification networks. \emph{Neural Computation}, 4(5):720--736, 1992.
\bibitem{wahba} G.~Wahba. Spline interpolation and smoothing on the sphere. \emph{SIAM J.\ Sci.\ Stat.\ Comput.}, 2(1):5--16, 1981.
\bibitem{jolliffe} I.~T.~Jolliffe. \emph{Principal Component Analysis}, wyd.~2. Springer, 2002.
\bibitem{cepeda} N.~J.~Cepeda i in. Distributed practice in verbal recall tasks: a review and quantitative synthesis. \emph{Psychological Bulletin}, 132(3):354--380, 2006.
\bibitem{cke} Centralna Komisja Egzaminacyjna. Arkusze egzaminu maturalnego z matematyki, poziom rozszerzony, 2002--2026.
\end{thebibliography}
"""


def _rys(sciezka: str, podpis: str, etykieta: str, szer: str = r"\linewidth", poz: str = "!htbp") -> str:
    return (rf"\begin{{figure}}[{poz}]\centering\includegraphics[width={szer}]{{{sciezka}}}"
            rf"\caption{{{podpis}}}\label{{{etykieta}}}\end{{figure}}")


def _rys2(lewy, prawy, podpis: str, etykieta: str) -> str:
    """Dwa panele (a), (b) obok siebie; lewy/prawy = (ścieżka, podpis panelu, etykieta)."""
    out = [r"\begin{figure}[!htbp]\centering"]
    for i, (s, p, e) in enumerate((lewy, prawy)):
        out.append(rf"\begin{{subfigure}}[t]{{.465\linewidth}}\centering\includegraphics[width=\linewidth]{{{s}}}"
                   rf"\caption{{{p}}}\label{{{e}}}\end{{subfigure}}" + (r"\hfill" if i == 0 else ""))
    out.append(rf"\caption{{{podpis}}}\label{{{etykieta}}}\end{{figure}}")
    return "".join(out)


def _zm(a, b, jedn="", odwrot=False):
    if a is None or b is None:
        return "---"
    d = round(a - b, 1)
    if d == 0:
        return "0"
    kol = "dobret" if (d > 0) != odwrot else "zle"
    return rf"\textcolor{{{kol}}}{{{'+' if d > 0 else '$-$'}{abs(d):g}{jedn}}}"



def _kpi_val(v, fmt="{}", brak="—"):
    return brak if v is None else fmt.format(v)


def _delta(a, b, jedn="", odwrot=False):
    if a is None or b is None:
        return "brak porównania"
    d = round(a - b, 1)
    if d == 0:
        return "bez zmian wobec poprz. tyg."
    dobrze = (d > 0) != odwrot
    kol = "dobret" if dobrze else "zle"
    return rf"\textcolor{{{kol}}}{{{'+' if d > 0 else '−'}{abs(d):g}{jedn}}} wobec poprz. tyg."


def _zdanie_trudnosci(prof) -> str:
    """Zdanie o wpływie trudności z raportu m.Profil (łatwość krajowa albo norma)."""
    lin = next((x for x in prof.get("raport", "").splitlines() if x.startswith("Trudność:")), "")
    lin = lin[len("Trudność:"):].strip()
    return _tex(lin[:1].upper() + lin[1:]) + " " if lin else ""


def _tag_oceny(o):
    return {"mocne": r"\textcolor{dobret}{mocne}", "slabe": r"\textcolor{zle}{słabe}",
            "niepewne": r"\textcolor{ink2}{za mało danych}"}[o]


def rekomendacje(D) -> list[str]:
    k, prof, out = D["kpi"], D["profil"], []
    if k["proby_tyg"] == 0:
        out.append("W tym tygodniu nie ma żadnej sprawdzonej próby. Prześlij zdjęcia rozwiązań z arkuszy; bez nich "
                   "profil i powtórki stoją w miejscu.")
    elif k["proby_tyg"] < 12:
        out.append(f"Tylko {k['proby_tyg']} sprawdzonych prób w tygodniu (plan: ok. 20 przy 5 lekcjach). Sprawdzaj "
                   "każdy arkusz tego samego dnia.")
    for x in prof.get("slabe", [])[:2]:
        out.append(f"Najsłabszy dział: {x['dzial']} ({x['opanowanie']}%, przedział {x['przedzial'][0]}–"
                   f"{x['przedzial'][1]}%). Arkusze dostaną z niego zadania o niższej trudności, dopóki nie przejdzie 55%.")
    for x in prof.get("do_sprawdzenia", [])[:2]:
        out.append(f"Za mało danych: {x['dzial']} (przedział {x['przedzial'][0]}–{x['przedzial'][1]}%). Jedno "
                   "zadanie diagnostyczne wystarczy, żeby zawęzić ocenę.")
    if k["powtorki"] >= 3:
        out.append(f"Zaległe powtórki: {k['powtorki']}. Pierwsze zadania kolejnych arkuszy to bliźniaki zadań z błędem.")
    ty = Counter(x["typ"] for x in D["tyg"] if x["typ"])
    if ty:
        t, n = ty.most_common(1)[0]
        rada = {"rachunkowy": "sprawdzaj każdy wynik podstawieniem lub szacunkiem",
                "warunki": "zaczynaj od zapisania dziedziny i założeń, zanim cokolwiek przekształcisz",
                "metoda": "zanim zaczniesz liczyć, zapisz jednym zdaniem plan rozwiązania",
                "pojęciowy": "przejrzyj kartę metod działu przed lekcją",
                "niedokończone": "zawsze kończ zdaniem z odpowiedzią",
                "odczyt": "podkreśl dane i pytanie w treści", "zapis": "pisz kolejne przekształcenia w osobnych linijkach"}[t]
        out.append(f"Najczęstszy błąd tygodnia: {t} (×{n}). Reguła: {rada}.")
    if k["sr_tyg"] is not None and k["sr_pop"] is not None and k["sr_tyg"] < k["sr_pop"] - 8:
        out.append(f"Średni wynik spadł z {k['sr_pop']:g}% do {k['sr_tyg']:g}%. Jeśli to nowy dział, to normalne; "
                   "jeśli nie, obniż trudność o 0.5.")
    dzis = D["dzis"]
    for d, v in sorted(D["harm"]["cele"].items()):
        dd = date.fromisoformat(d)
        if 0 <= (dd - dzis).days <= 21:
            out.append(f"Za {(dd - dzis).days} dni próg fazy: {v} (do {dd:%d.%m}).")
            break
    if D["nast"].get("pelny"):
        out.append(f"W przyszłym tygodniu pełny arkusz {D['nast']['pelny']} (wtorek, dwie lekcje). Zaplanuj sen.")
    return out or ["Wszystko według planu: kontynuuj bieżący blok."]


def zloz_tex(D, wyk: dict, komentarz: str | None, symulacja: bool) -> str:
    k, prof, t, nast = D["kpi"], D["profil"], D["tydz"], D["nast"]
    L = [PREAMBULA.replace("@@NAGLOWEK@@", f"tydzień {t['tydzien']}, {D['od']:%d.%m}–{D['dzis']:%d.%m.%Y}")]
    A = L.append
    ok_prof = prof.get("status") == "ok"
    pk = k["pkt_tyg"]

    # ---------------------------------------------------------------- tytuł i streszczenie
    A(r"\thispagestyle{plain}\begin{center}")
    A(r"{\LARGE\bfseries Modelowanie opanowania materiału maturalnego\\[2pt] z matematyki rozszerzonej: tydzień "
      + str(t["tydzien"]) + r" z 29\par}\vspace{8pt}")
    A(r"{\large uczeń\textsuperscript{1} \qquad Claude\textsuperscript{2}\par}\vspace{3pt}")
    A(r"{\small\textsuperscript{1}Uczeń, matura 2027 \qquad \textsuperscript{2}Korepetytor, system \texttt{matura.db}\par}\vspace{3pt}")
    A(r"{\small " + _tex(_data_pl(D["dzis"])) + r" \textbullet{} okres " + f"{D['od']:%d.%m}–{D['dzis']:%d.%m.%Y}"
      + r" \textbullet{} faza: " + _tex(t["faza"]) + r"\par}")
    if symulacja:
        A(r"\vspace{4pt}{\small\bfseries\color{zle}RAPORT TESTOWY NA DANYCH SYMULOWANYCH: to nie są wyniki ucznia.\par}")
    A(r"\end{center}\vspace{-2pt}")

    abst = []
    if k["proby_tyg"]:
        abst.append(f"Analizujemy {k['proby_tyg']} sprawdzonych prób z {k['dni_akt']} dni tygodnia {t['tydzien']} "
                    f"(łącznie {k['lacznie']} prób z {k['zadan']} zadań CKE~\\cite{{cke}}). "
                    f"Średni wynik zadania wyniósł {k['sr_tyg']:g}\\%"
                    + (f" wobec {k['sr_pop']:g}\\% tydzień wcześniej." if k["sr_pop"] is not None else "."))
    else:
        abst.append("W analizowanym tygodniu nie zarejestrowano sprawdzonych prób; raport opisuje stan modelu sprzed tygodnia.")
    abst.append(r"Opanowanie 15 działów szacujemy ważoną regresją logistyczną z karą ridge i poprawką na trudność zadania, "
                r"z 80\% przedziałami z przybliżenia Laplace'a; kompetencje z 160-wymiarowej przestrzeni zadań "
                r"rzutujemy na sferę i wygładzamy harmonikami sferycznymi.")
    if ok_prof:
        if prof["slabe"]:
            abst.append("Najsłabsze działy: " + _tex(", ".join(f"{x['dzial']} ({x['opanowanie']}%)" for x in prof["slabe"][:3])) + ".")
        if prof["mocne"]:
            abst.append("Najmocniejsze: " + _tex(", ".join(f"{x['dzial']} ({x['opanowanie']}%)" for x in prof["mocne"][:3])) + ".")
    if k["gotowosc"] is not None:
        abst.append(f"Średnie przewidywane opanowanie wynosi {k['gotowosc']}\\% przy {k['dni_do_matury']} dniach do egzaminu.")
    A(r"\begin{abstract}\noindent " + " ".join(abst) + r"\par\smallskip\noindent\textbf{Słowa kluczowe:} "
      r"knowledge tracing, regresja logistyczna, przybliżenie Laplace'a, harmoniki sferyczne, powtórki rozłożone w czasie.\end{abstract}")

    # ---------------------------------------------------------------- 1. wstęp
    A(r"\section{Wstęp}")
    A(r"Celem jest wynik 100\% na maturze rozszerzonej z matematyki (11.05.2027). Każdy tydzień to iteracja: arkusz "
      r"dobrany z bazy, sprawdzenie rozwiązań, aktualizacja modelu ucznia. Raport podaje stan modelu (sekcja~\ref{sec:metody}), "
      r"wyniki tygodnia (sekcja~\ref{sec:wyniki}) i wnioski na kolejny tydzień (sekcja~\ref{sec:dyskusja}). "
      r"Podejście jest pokrewne modelom Rascha~\cite{rasch} i śledzeniu wiedzy~\cite{corbett}.")

    # ---------------------------------------------------------------- 2. dane
    A(r"\section{Dane}\label{sec:dane}")
    A(r"Źródłem jest tabela \texttt{history} bazy \texttt{matura.db}: każda próba ma identyfikator zadania, ocenę z punktów, "
      r"typ błędu i czas. Każde zadanie opisuje 160-wymiarowy wektor (działy, umiejętności, trudność); jego długość "
      r"(norma) była pierwotną miarą trudności. Od 23.10.2026 trudność to łatwość krajowa ze sprawozdań CKE "
      r"(odsetek punktów zdobytych w zadaniu przez wszystkich zdających, maj 2015--2026), a dla zadań bez danych jej szacunek "
      r"z punktacji, pozycji w arkuszu, normy i 7 najbliższych zadań z danymi. Tabela~\ref{tab:kpi} streszcza tydzień.")
    A(r"\begin{table}[!htbp]\centering\caption{Statystyki tygodnia na tle tygodnia poprzedniego.}\label{tab:kpi}\small")
    A(r"\begin{tabular}{@{}lrrr@{}}\toprule Miara & Ten tydzień & Poprzedni & Zmiana\\\midrule")
    A(f"Sprawdzone próby & {k['proby_tyg']} & {k['proby_pop']} & {_zm(k['proby_tyg'], k['proby_pop'])}\\\\")
    A(f"Średni wynik zadania (\\%) & {_kpi_val(k['sr_tyg'], '{:g}')} & {_kpi_val(k['sr_pop'], '{:g}')} & {_zm(k['sr_tyg'], k['sr_pop'], ' pp')}\\\\")
    A(f"Punkty z zadań & {f'{pk[0]:g}/{pk[1]}' if pk[1] else '---'} & & \\\\")
    A(f"Komplet / częściowo / zero & {k['ok']} / {k['partial']} / {k['fail']} & & \\\\")
    A(f"Dni aktywne & {k['dni_akt']} & & \\\\\\midrule")
    A(f"Średnie opanowanie z modelu (\\%) & {_kpi_val(k['gotowosc'])} & & \\\\")
    A(f"Próby / zadania w historii & {k['lacznie']} / {k['zadan']} & & \\\\")
    A(f"Zaległe powtórki & {k['powtorki']} & & \\\\")
    A(f"Dni do matury & {k['dni_do_matury']} & & \\\\\\bottomrule\\end{{tabular}}\\end{{table}}")
    if D["arkusze"]:
        A(r"\begin{table}[!htbp]\centering\caption{Arkusze wygenerowane w tygodniu.}\label{tab:arkusze}\small")
        A(r"\begin{tabular}{@{}lrrl@{}}\toprule Folder & Zadań & Sprawdzone & Status\\\midrule")
        for a in D["arkusze"]:
            st = "WYNIK.md gotowy" if a["wynik"] else ("sprawdzony" if a["sprawdzone"] == a["zadan"] else
                                                       ("czeka na sprawdzenie" if a["sprawdzone"] == 0 else "sprawdzony częściowo"))
            A(f"{_tex(a['folder'])} & {a['zadan']} & {a['sprawdzone']} & {st}\\\\")
        A(r"\bottomrule\end{tabular}\end{table}")

    # ---------------------------------------------------------------- 3. metody
    A(r"\section{Metody}\label{sec:metody}")
    A(r"\paragraph{Model opanowania.} Każda próba to wynik ułamkowy (punkty zdobyte przez maksymalne). Szansę na komplet "
      r"punktów modelujemy regresją logistyczną~\cite{hastie}, w której wynik zależy od umiejętności działu, odchyleń "
      r"poszczególnych umiejętności od działu i trudności zadania. Kara ridge ściąga oceny do zera przy małej liczbie prób, "
      r"a starsze próby ważą mniej (połowa wagi po 30 dniach). Niepewność daje przybliżenie Laplace'a~\cite{mackay}: "
      r"każdy dział ma 80\% przedział, a ocenę \emph{mocny} lub \emph{słaby} dostaje tylko wtedy, gdy cały przedział "
      r"leży po jednej stronie progu.")
    A(r"\paragraph{Trudność z łatwości krajowej.} Kolumną trudności w modelu jest $\operatorname{logit}(p_i) - \operatorname{logit}(\bar p_r)$ "
      r"przesunięte do zadania o łatwości 43\% (dawna norma 3,5), gdzie $p_i$ to łatwość zadania, a $\bar p_r$ średnia arkusza z jego roku. "
      r"Szacunek łatwości dla zadań bez danych ma błąd ok. 11~pkt proc. (walidacja z pominięciem jednego zadania); "
      r"symulacja na 60 uczniach dała zgodność kolejności działów nie gorszą niż z normą.")
    A(r"\paragraph{Sfera kompetencji.} Wektory zadań rzutujemy metodą PCA~\cite{jolliffe} do trzech wymiarów i na sferę; "
      r"wyniki ostatnich prób wygładzamy harmonikami sferycznymi z karą za krzywiznę~\cite{wahba}. "
      r"Doliny tej powierzchni to luki w wiedzy, szczyty to opanowany materiał.")
    A(r"\paragraph{Powtórki.} Zadania z błędem wracają po 3, 7 i 21 dniach, zgodnie z efektem rozłożenia w czasie~\cite{cepeda}.")

    # ---------------------------------------------------------------- 4. wyniki
    A(r"\section{Wyniki}\label{sec:wyniki}")
    A(r"\subsection{Aktywność}")
    if "trend" in wyk:
        A(r"Rys.~\ref{fig:trend} pokazuje wynik w czasie na tle progów faz, a Rys.~\ref{fig:akt} rozkład aktywności i wyników tygodnia.")
        A(_rys(wyk["trend"], r"Wynik zadania w czasie: dzienna średnia, średnia krocząca z 7 dni i progi faz 60/75/90\%; "
               r"dolny panel to liczba prób w tygodniu.", "fig:trend"))
    else:
        A(r"\brak{Wykres wyniku w czasie pojawi się po pierwszej sprawdzonej próbie.}")
    if "tydzien_dzialy" in wyk:
        A(_rys2((wyk["aktywnosc"], "Próby dziennie, ostatnie 8 tygodni.", "fig:akt-a"),
                (wyk["tydzien_dzialy"], "Wyniki tygodnia w działach: kółko to jedna próba, romb to średnia.", "fig:akt-b"),
                "Aktywność i rozkład wyników tygodnia.", "fig:akt"))
    else:
        A(_rys(wyk["aktywnosc"], "Próby dziennie, ostatnie 8 tygodni.", "fig:akt", szer=r".55\linewidth"))

    A(r"\subsection{Profil umiejętności}")
    if ok_prof:
        b = prof.get("trudnosc_b")
        A(r"Rys.~\ref{fig:profil} zestawia opanowanie działów z modelu oraz zależność wyniku od trudności. "
          + _zdanie_trudnosci(prof))
        if "trudnosc" in wyk:
            A(_rys2((wyk["profil"], r"Opanowanie działów; jasny pasek to 80\% przedział, w nawiasie liczba prób.", "fig:profil-a"),
                    (wyk["trudnosc"], r"Wynik a trudność; krzywa to przewidywanie modelu z 80\% pasmem niepewności.", "fig:profil-b"),
                    "Profil umiejętności ucznia.", "fig:profil"))
        else:
            A(_rys(wyk["profil"], r"Opanowanie działów z 80\% przedziałem.", "fig:profil", szer=r".6\linewidth"))
        nietkniete = sorted({d["makro_dzial"] for d in D["engine"].dims_list if d.get("segment") == "A"} - {x["dzial"] for x in prof["dzialy"]})
        if nietkniete:
            A(r"Działy bez prób: " + _tex(", ".join(nietkniete)) + ".")
        um = [x for x in prof.get("umiejetnosci", []) if x["proby"] >= 2]
        if um:
            A(r"\begin{table}[!htbp]\centering\caption{Najsłabsze i najmocniejsze umiejętności (co najmniej 2 próby).}\label{tab:um}\footnotesize")
            A(r"\begin{tabularx}{\linewidth}{@{}Xrr@{\hspace{14pt}}Xrr@{}}\toprule Najsłabsze & \% & Próby & Najmocniejsze & \% & Próby\\\midrule")
            sl, mo = um[:6], um[::-1][:6]
            for i in range(max(len(sl), len(mo))):
                a_ = f"{_tex(sl[i]['umiejetnosc'][:60])} & {sl[i]['opanowanie']} & {sl[i]['proby']}" if i < len(sl) else "& &"
                b_ = f"{_tex(mo[i]['umiejetnosc'][:60])} & {mo[i]['opanowanie']} & {mo[i]['proby']}" if i < len(mo) else "& &"
                A(a_ + " & " + b_ + r"\\")
            A(r"\bottomrule\end{tabularx}\end{table}")
        if "zmiana_profilu" in wyk or "ewolucja3d" in wyk:
            A(r"Dynamikę profilu pokazują Rys.~\ref{fig:dyn} i~\ref{fig:ewol}.")
        if "zmiana_profilu" in wyk:
            A(_rys(wyk["zmiana_profilu"], "Zmiana opanowania w ciągu tygodnia: puste kółko to stan sprzed 7 dni, pełne to stan obecny.",
                   "fig:dyn", szer=r".5\linewidth"))
    else:
        A(r"\brak{Profil działów powstaje z historii prób; pojawi się po kilku sprawdzonych arkuszach.}")

    A(r"\subsection{Na tle kraju}")
    kr = D["kraj"]
    if kr["zadania"]:
        pk, oc, mx = (sum(x[k_] for x in kr["dzialy"]) for k_ in ("pkt", "oczek", "maks"))
        A(f"W {kr['zadania']} zadaniach z łatwością krajową (ostatnia próba każdego; szacunek dla {kr['szacunek']}) "
          f"zdobyto {pk:.0f}/{mx} pkt ({100 * pk / mx:.0f}\\%), a przeciętny maturzysta zdobyłby "
          f"{'ok. ' if kr['szacunek'] else ''}{oc:.1f} pkt ({100 * oc / mx:.0f}\\%). Tab.~\\ref{{tab:kraj}} rozbija to na działy.")
        tr = kr["trudne"]
        if tr["zadania"]:
            A(f"Kluczowa miara dla wyniku 100\\%: w zadaniach trudnych dla kraju (łatwość poniżej 30\\%) "
              f"{tr['pkt']:.0f}/{tr['maks']} pkt, czyli {100 * tr['pkt'] / tr['maks']:.0f}\\% ({tr['zadania']} zadań).")
        else:
            A(r"Zadań trudnych dla kraju (łatwość poniżej 30\%) jeszcze nie było.")
        A(r"\begin{table}[!htbp]\centering\caption{Punkty na tle przeciętnego maturzysty (suma łatwości razy punkty zadania).}\label{tab:kraj}\small")
        A(r"\begin{tabular}{@{}lrrrr@{}}\toprule Dział & Zadań & Punkty & Przeciętny & Różnica\\\midrule")
        for x in kr["dzialy"]:
            A(f"{_tex(_skrot(x['dzial'], 30))} & {x['zadania']} & {x['pkt']:.0f}/{x['maks']} & {x['oczek']:.1f} & {x['pkt'] - x['oczek']:+.1f}\\\\")
        A(r"\bottomrule\end{tabular}\end{table}")
    else:
        A(r"\brak{Żadne zadanie z historii nie ma łatwości krajowej (tabela latwosc albo szacunek).}")
    for x in D["pelne"]:
        z = kr["zestawy_cke"].get(x["zestaw"])
        if z and x["proc"] is not None:
            A(f"Matura {x['zestaw'][4:8]} ({_tex(x['zestaw'])}): {x['proc']}\\% wobec średniej kraju {z['srednia']}\\% "
              f"(mediana {z['mediana']}\\%, {z['zdajacych']:,} zdających).".replace(",", "~").replace("%~", "%,"))
        if x["proc"] is not None and D["m"].Centyl(x["proc"]) is not None:
            A(f"Pełny arkusz {_tex(x['zestaw'])}: {x['proc']}\\% to wynik lepszy niż ok. {D['m'].Centyl(x['proc'])}\\% zdających rozszerzoną.")
    pg = D["prognoza"]
    if pg["status"] == "ok":
        A(f"Prognoza matury z {pg['proby']} prób (model profilu na zadaniach matur "
          f"{', '.join(map(str, pg['arkusze']))}, z łatwością CKE): \\textbf{{{pg['prognoza']}\\%}} "
          f"(80\\% przedział {pg['przedzial'][0]}--{pg['przedzial'][1]}\\%), czyli lepiej niż ok. {pg['centyl']}\\% zdających.")
    pw_ = D["pewniaki"]
    A(_tex(pw_["tekst"]))

    A(r"\subsection{Reprezentacje trójwymiarowe}")
    if "krajobraz3d" in wyk:
        A(r"Rys.~\ref{fig:krajobraz} rozkłada model na płaszczyznę dział $\times$ trudność; spadek wzdłuż osi "
          r"trudności wskazuje poziom, od którego dział przestaje być opanowany.")
        A(_rys(wyk["krajobraz3d"], r"Krajobraz szans: przewidywana szansa na komplet punktów w funkcji działu i trudności zadania. "
               + (r"Przerywane linie to przekroje dla łatwości krajowej 60, 40 i 20\%" if D["kraj"]["model_lat"]
                  else r"Przerywane linie to przekroje dla trudności 3, 4 i 5")
               + r"; kolor czerwony oznacza szansę poniżej 50\%.", "fig:krajobraz"))
    else:
        A(r"\brak{Krajobraz szans pojawi się po pierwszych próbach.}")
    if "ewolucja3d" in wyk:
        A(_rys(wyk["ewolucja3d"], r"Ewolucja opanowania: model dopasowany do historii do końca każdego tygodnia.", "fig:ewol"))
    if "sfera3d" in wyk:
        sf, st = D["sfera"], D["sfera"]["stats"]
        A(r"Sfera kompetencji (Rys.~\ref{fig:sfera}) ma stopień harmonik " + f"{st['stopien_L']}, "
          + f"błąd walidacji krzyżowej ±{st['margines_bledu']} pkt na 10, R² = {st['r2']} i pokrycie sfery {_tex(st['pokrycie_sfery'])} "
          + f"(wiarygodność: {_tex(st['wiarygodnosc'])}). Punkty krytyczne zestawia Tab.~\\ref{{tab:kryt}}.")
        A(_rys(wyk["sfera3d"], r"Sfera kompetencji: rzut PCA wektorów zadań na sferę z wygładzeniem harmonikami "
               r"sferycznymi, dwa widoki; punkty to zadania z historii.", "fig:sfera"))
        if sf["kryt"]:
            A(r"\begin{table}[!htbp]\centering\caption{Luki i mocne strony na sferze kompetencji.}\label{tab:kryt}\small")
            A(r"\begin{tabularx}{\linewidth}{@{}llXr@{}}\toprule & Wynik & Czego dotyczy (główne umiejętności) & Zadanie\\\midrule")
            for cp in sf["kryt"]:
                cz = re.split(r"\s*\((\d+(?:\.\d+)?)%\),?\s*", cp["opis"])
                nazwy = [n.strip() for n in cz[0::2] if n.strip()][:2]
                nazwy = [n if len(n) <= 70 else n[:68].rsplit(" ", 1)[0] + "…" for n in nazwy]
                typ = r"\textcolor{zle}{luka}" if cp["typ"] == "MINIMUM" else r"\textcolor{dobret}{mocna}"
                A(f"{typ} & {cp['wysokosc']:.1f}/10 & {_tex('; '.join(nazwy))} & {cp.get('rekomendowane_zadanie_id') or '—'}" + r"\\")
            A(r"\bottomrule\end{tabularx}\par\smallskip{\footnotesize\color{ink2}Wynik to wysokość sfery w tym miejscu (0–10). "
              r"Przy luce podane jest zadanie z bazy, które ją ćwiczy.}\end{table}")
    else:
        A(r"\brak{Sfera kompetencji wymaga co najmniej 3 różnych zadań w historii.}")
    A(r"Rys.~\ref{fig:pca} umieszcza zrobione zadania w przestrzeni całej bazy (ok. " + f"{round(100 * D['engine'].pca_var_ratio)}"
      + r"\% wariancji w trzech składowych); puste rejony chmury to typy zadań jeszcze nieprzećwiczone.")
    if "czas_trudnosc3d" in wyk:
        A(_rys2((wyk["przestrzen3d"], "Przestrzeń zadań bazy (PCA); kolorowe: zadania z historii.", "fig:pca-a"),
                (wyk["czas_trudnosc3d"], r"Czas $\times$ trudność $\times$ wynik z płaszczyzną trendu (MNK).", "fig:pca-b"),
                "Zadania w przestrzeni pojęć i w czasie.", "fig:pca"))
    else:
        A(_rys(wyk["przestrzen3d"], "Przestrzeń zadań bazy (PCA); kolorowe: zadania z historii.", "fig:pca", szer=r".55\linewidth"))

    A(r"\subsection{Analiza błędów}")
    bl = [x for x in D["tyg"] if x["wynik"] != "ok"]
    if "bledy" in wyk and "dzial_typ_bledu" in wyk:
        A(r"Najczęstsze typy błędów i działy, w których występują, pokazuje Rys.~\ref{fig:bledy}.")
        A(_rys2((wyk["bledy"], "Typy błędów (kółko: od początku; romb: ten tydzień, jeśli się różni).", "fig:bledy-a"),
                (wyk["dzial_typ_bledu"], r"Gdzie występują błędy: wiersz to dział, kolumna to typ, wielkość kropki to liczba błędów.", "fig:bledy-b"),
                "Struktura błędów.", "fig:bledy"))
    elif "bledy" in wyk:
        A(_rys(wyk["bledy"], "Typy błędów: ten tydzień i całość.", "fig:bledy", szer=r".55\linewidth"))
    if bl:
        A(r"\begin{table}[!htbp]\centering\caption{Próby z błędem w tygodniu" + (f" (ostatnie 16 z {len(bl)})" if len(bl) > 16 else "")
          + r".}\label{tab:bledy}\scriptsize")
        A(r"\begin{tabularx}{\linewidth}{@{}llrlrlX@{}}\toprule Dzień & Zadanie & id & Dział & Pkt & Typ & Opis\\\midrule")
        for x in bl[-16:]:
            d = _dt(x["ts"])
            pk_ = f"{x['pkt']:g}/{x['maks']}" if x["maks"] else x["wynik"]
            A(f"{DNI_PL[d.weekday()]} {d:%d.%m} & {_tex(x['nazwa'][-22:])} & {x['id']} & {_tex(_skrot(x['dzial'], 18))} & {pk_} & {_tex(x['typ'] or '—')} & {_tex(x['opis'][:70])}\\\\")
        A(r"\bottomrule\end{tabularx}\end{table}")
    elif not ("bledy" in wyk):
        A(r"\brak{W tym tygodniu brak zadań z błędem albo brak sprawdzonych prób.}")
    if D["powt"]:
        A(r"Kolejkę zaległych powtórek~\cite{cepeda} podaje Rys.~\ref{fig:powt}.")
        if "powtorki" in wyk:
            A(_rys(wyk["powtorki"], "Zaległe powtórki: dni po terminie (czerwone: ponad 7 dni).", "fig:powt", szer=r".45\linewidth"))
    else:
        A(r"Zaległych powtórek: 0.")

    # ---------------------------------------------------------------- 5. dyskusja
    A(r"\FloatBarrier\section{Dyskusja}\label{sec:dyskusja}")
    if komentarz:
        A("\n\n".join(_tex_z_math(a.strip()) for a in komentarz.strip().split("\n\n") if a.strip()))
    A(r"\paragraph{Rekomendacje na tydzień " + str(nast["tydzien"]) + r".}")
    A(r"\begin{enumerate}")
    for r_ in rekomendacje(D):
        A(r"\item " + _tex(r_))
    A(r"\end{enumerate}")
    A(r"\paragraph{Ograniczenia.} Wynik ułamkowy traktujemy jak prawdopodobieństwo (quasi-binomialnie); "
      r"tekst zadań w bazie pochodzi z OCR, więc wektory części zadań są zaszumione; przy małej liczbie prób przedziały są szerokie "
      r"i dział trafia do kategorii ,,za mało danych''. Ocena punktowa pochodzi z jednego sprawdzającego.")

    # ---------------------------------------------------------------- 6. plan
    A(r"\section{Dalsza praca}")
    A(r"Rys.~\ref{fig:harm} przedstawia fazy przygotowań i progi pełnych arkuszy (Tab.~\ref{tab:progi}).")
    A(_rys(wyk["harmonogram"], "Harmonogram 29 tygodni z fazami, progami i pełnymi arkuszami.", "fig:harm"))
    A(r"\begin{table}[!htbp]\centering\caption{Progi faz i pełne arkusze.}\label{tab:progi}\small")
    A(r"\begin{tabularx}{\linewidth}{@{}lXr@{}}\toprule Do dnia & Próg & Pozostało\\\midrule")
    for d, v in sorted(D["harm"]["cele"].items()):
        dd = date.fromisoformat(d)
        dn = (dd - D["dzis"]).days
        A(f"{dd:%d.%m.%Y} & {_tex(v)} & {str(dn) + ' dni' if dn >= 0 else 'minął'}\\\\")
    A(r"\midrule")
    if D["pelne"]:
        for x in D["pelne"]:
            A(f"{date.fromisoformat(x['data']):%d.%m.%Y} & arkusz {_tex(x['zestaw'])}: {_tex(x['pkt'])}"
              + (f" ({x['proc']}\\%)" if x["proc"] is not None else "") + (f"; stracone: {_tex(x['stracone'])}" if x["stracone"] else "") + r" & \\")
    else:
        A(r"\multicolumn{3}{@{}l}{Pełne arkusze: jeszcze żaden (pierwszy CKE\_2025\_Z01 w tygodniu 2).}\\")
    A(r"\bottomrule\end{tabularx}\end{table}")
    zd = [f"Tydzień {nast['tydzien']} ({date.fromisoformat(nast['od']):%d.%m}–{date.fromisoformat(nast['do']):%d.%m}): "
          + r"\textbf{" + _tex(nast["faza"]) + r"}, " + _tex(nast["blok"]) + "."]
    kl = [next(iter(x)) if isinstance(x, dict) else str(x) for x in nast.get("klucze", [])]
    if kl:
        zd.append("Tematy: " + _tex(", ".join(kl)) + ".")
    ln = nast.get("length")
    if ln:
        zd.append(f"Zakres trudności (length) {ln[0]:g}–{ln[1]:g}.")
    if nast.get("pelny"):
        zd.append(r"Pełny arkusz: " + _tex(nast["pelny"]) + " (wtorek, dwie lekcje).")
    if nast.get("uwagi"):
        zd.append(_tex(nast["uwagi"]))
    lek = D["harm"].get("lekcje", {})
    if lek:
        zd.append("Lekcje: " + _tex(", ".join(f"{d} {' i '.join(g)}" for d, g in lek.items())) + ".")
    A(" ".join(zd))

    # ---------------------------------------------------------------- bibliografia i dostępność
    A(r"\FloatBarrier")
    A(BIBLIOGRAFIA)
    A(r"\paragraph{Dostępność danych i kodu.} Dane: \texttt{matura.db}, \texttt{dane/harmonogram.json}, \texttt{notatki/postep.md}, "
      r"\texttt{arkusze/}. Raport wygenerowano " + f"{_dt(time.time()):%d.%m.%Y %H:%M}"
      + r" skryptem \texttt{matura.py raport} (tylko odczyt bazy); wynik zadania: punkty z notatki, a bez nich ok = 100\%, "
      r"partial = 50\%, fail = 10\%.")
    A(r"\end{document}")
    return "\n".join(L)


# ============================================================================ główna

def _main_raport(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dzien", help="data raportu RRRR-MM-DD (domyślnie dziś w Polsce)")
    ap.add_argument("--symulacja", action="store_true", help="symulowany uczeń na kopii bazy")
    ap.add_argument("--komentarz", help="plik z komentarzem (akapity rozdzielone pustą linią; $...$ wolno)")
    ap.add_argument("--out", help="folder wyjściowy (domyślnie raporty/RRRR-MM-DD_tydzien)")
    a = ap.parse_args(argv)
    zapewnij_zaleznosci()
    dzien = date.fromisoformat(a.dzien) if a.dzien else datetime.now(TZ).date()
    teraz = time.time() if not a.dzien else datetime(dzien.year, dzien.month, dzien.day, 9, 47, tzinfo=TZ).timestamp()
    if a.symulacja:
        if not a.dzien:
            dzien = date(2026, 11, 21)
            teraz = datetime(2026, 11, 21, 9, 47, tzinfo=TZ).timestamp()
        kat_bazy = przygotuj_symulacje(dzien)
    else:
        kat_bazy = BASE
        sys.path.insert(0, str(BASE))
    import matura as m
    p = m
    assert Path(m.__file__).resolve().parent == kat_bazy.resolve(), "zły katalog modułów"
    out = Path(a.out) if a.out else RAPORTY / (dzien.isoformat() + ("_tydzien_sym" if a.symulacja else "_tydzien"))
    out.mkdir(parents=True, exist_ok=True)
    D = zbierz(m, p, teraz)
    wyk = wykresy(D, out / "wykresy")
    kom = Path(a.komentarz).read_text(encoding="utf-8") if a.komentarz else None
    tex = zloz_tex(D, wyk, kom, a.symulacja)
    (out / "raport.tex").write_text(tex, encoding="utf-8")
    for _ in range(2):
        r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "raport.tex"], cwd=out,
                           capture_output=True, text=True, errors="replace")
        if r.returncode:
            print(r.stdout[-3000:])
            raise SystemExit("[raport] BŁĄD: pdflatex się nie powiódł (log: raport.log)")
    for f in ("raport.aux", "raport.out"):
        (out / f).unlink(missing_ok=True)
    pod = out / "podglad"
    shutil.rmtree(pod, ignore_errors=True)
    pod.mkdir()
    if shutil.which("pdftoppm"):
        subprocess.run(["pdftoppm", "-png", "-r", "70", str(out / "raport.pdf"), str(pod / "strona")], check=False)
    k = D["kpi"]
    dane = {"dzien": dzien.isoformat(), "symulacja": a.symulacja, "kpi": k,
            "profil": {x["dzial"]: {kk: x[kk] for kk in ("opanowanie", "przedzial", "proby", "ocena")}
                       for x in D["profil"].get("dzialy", [])},
            "rekomendacje": rekomendacje(D), "tydzien": D["tydz"]["tydzien"], "faza": D["tydz"]["faza"],
            "blok": D["tydz"]["blok"], "nastepny_blok": D["nast"]["blok"], "pelne_arkusze": D["pelne"],
            "wykresy": sorted(wyk), "strony": len(list(pod.glob("*.png"))),
            "na_tle_kraju": {kk: D["kraj"][kk] for kk in ("zadania", "szacunek", "dzialy", "trudne")},
            "prognoza": {kk: D["prognoza"].get(kk) for kk in ("prognoza", "przedzial", "centyl", "arkusze")},
            "pewniaki_braki": D["pewniaki"]["braki"]}
    (out / "dane.json").write_text(json.dumps(dane, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if a.symulacja:
        shutil.rmtree(kat_bazy, ignore_errors=True)
    print(f"[raport] OK: {out / 'raport.pdf'} ({dane['strony']} str., {k['proby_tyg']} prób w tygodniu, {k['lacznie']} łącznie)")
    return out


# ==============================================================================
# SEKCJA: raport_atlas (dawniej raport_atlas.py)
# ==============================================================================

# Atlas bazy zadań: raport z WSZYSTKICH zadań bazy (nie tylko prób ucznia), głównie wykresy 3D.
#
#     python3 raport_atlas.py
#     python3 raport_atlas.py --symulacja     # test na symulowanym uczniu
#
# Wynik: raporty/RRRR-MM-DD_atlas/raport.pdf (+ wykresy/, podglad/, dane.json).
# Importuje matura.py raport (nie zmienia go). Tylko czyta bazę. Próby ucznia są nałożone na mapę całej bazy.



_tex_raport_atlas, _dt_raport_atlas, _skrot_raport_atlas = _tex, _dt, _skrot
PAL = ["#2E86AB", "#E4572E", "#17BEBB", "#F2A541", "#A23B72", "#5B8E3E", "#6A4C93", "#D1495B",
       "#3D5A80", "#8D6A9F", "#2F9E7A", "#C98B2B", "#7A7A7A"]
KOL_WYNIK = {"ok": "#12A150", "partial": "#F59E0B", "fail": "#DC2626"}
NAZ_WYNIK = {"ok": "komplet", "partial": "częściowo", "fail": "zero"}


def rok(nazwa):
    mt = re.match(r"CKE_(\d{4})", nazwa or "")
    return int(mt.group(1)) if mt else None


def zbierz_atlas(m, p, teraz):
    import numpy as np
    import sqlite3
    D = zbierz(m, p, teraz)
    e = D["engine"]
    con = sqlite3.connect(Path(m.DB))
    nazwy = dict(con.execute("SELECT id, name FROM desc").fetchall())
    con.close()
    ids = [int(i) for i in e.ids.tolist()]
    A = {"ids": np.array(ids), "dzial": np.array([(p.Dzial(i) if p.Dzial(i) not in ("?", "", None) else "bez działu") for i in ids]), "norma": np.array(e.norms, dtype=float),
         "rok": np.array([rok(nazwy.get(i, "")) or 0 for i in ids]), "idx": {i: k for k, i in enumerate(ids)}}
    A["maska"] = A["norma"] > 0.5
    ost = {}
    for x in sorted(D["proby"], key=lambda x: x["ts"]):
        ost[x["id"]] = x
    A["ost"] = ost
    D["A"] = A
    return D


def wykresy_atlas(D, kat: Path):
    import numpy as np
    plt = _styl()
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.lines import Line2D
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    kat.mkdir(parents=True, exist_ok=True)
    out = {}
    e, A, m = D["engine"], D["A"], D["m"]
    ost = A["ost"]
    dzialy = [d for d, _ in Counter(A["dzial"][A["maska"]]).most_common()]
    kol = {d: PAL[i % len(PAL)] for i, d in enumerate(dzialy)}
    KD = np.array([kol.get(d, "#999999") for d in A["dzial"]])
    ok_idx = np.where(A["maska"])[0]
    prob_idx = np.array([A["idx"][i] for i in ost]) if ost else np.array([], dtype=int)
    prob_kol = [KOL_WYNIK[x["wynik"]] for x in ost.values()]
    dz_ord = sorted(dzialy, key=lambda d: np.mean(A["norma"][(A["dzial"] == d) & A["maska"]]))

    def zapisz(fig, nazwa):
        fig.savefig(kat / f"{nazwa}.pdf", dpi=200)
        plt.close(fig)
        out[nazwa] = f"wykresy/{nazwa}.pdf"

    def panele(ax):
        for a in (ax.xaxis, ax.yaxis, ax.zaxis):
            a.pane.set_facecolor("#fafaf8")
            a.pane.set_edgecolor("#e6e5df")
            a._axinfo["grid"]["color"] = "#e6e5df"
            a._axinfo["grid"]["linewidth"] = .35

    def bez_osi(ax):
        panele(ax)
        for a in (ax.xaxis, ax.yaxis, ax.zaxis):
            a.set_ticklabels([])
            a.line.set_color("#cfcdc4")
        ax.tick_params(length=0)

    def stopka(fig, dzial=True, prob=True):
        """Legendy w osobnym pasku na dole rysunku (osie zaczynają się wyżej, więc nic na siebie nie nachodzi)."""
        y = .01
        if prob:
            h = [Line2D([], [], marker="*", ls="", mfc=KOL_WYNIK[k], mec="#111", ms=7, mew=.5, label=f"próba: {v}")
                 for k, v in NAZ_WYNIK.items()]
            fig.legend(handles=h, loc="lower center", ncol=3, fontsize=6.5, frameon=False, bbox_to_anchor=(.5, y))
            y += .05
        if dzial:
            h = [Line2D([], [], marker="o", ls="", color=kol[d], ms=4.5, label=_skrot_raport_atlas(d, 24)) for d in dzialy]
            fig.legend(handles=h, loc="lower center", ncol=7, fontsize=6.2, frameon=False, bbox_to_anchor=(.5, y),
                       columnspacing=.9, handletextpad=.2)
            y += .08
        return y

    def od_tylu(punkty, az, el, aspekt):
        """Indeksy punktów (x, y) posortowane od najdalszych do najbliższych kamery; aspekt = proporcje pudełka osi (x, y)."""
        P_ = np.array(punkty, dtype=float)
        P_ = (P_ - (P_.max(0) + P_.min(0)) / 2) / np.maximum(np.ptp(P_, axis=0), 1e-9) * np.array(aspekt)
        c = np.array([np.cos(np.radians(az)), np.sin(np.radians(az))])
        return np.argsort(P_ @ c)

    def gwiazdy(ax, X, Y, Z, s=75):
        if len(X):
            ax.scatter(X, Y, Z, s=s, marker="*", c=prob_kol, edgecolor="#111", linewidth=.5, depthshade=False, zorder=10)

    # ---------- 1. mapa całej bazy (t-SNE 3D), jeden duży widok, kolor = dział
    from sklearn.manifold import TSNE
    from sklearn.decomposition import PCA
    V = e.vectors[ok_idx]
    Vn = V / np.linalg.norm(V, axis=1, keepdims=True)
    Z50 = PCA(n_components=min(50, Vn.shape[1]), random_state=0).fit_transform(Vn)
    T = TSNE(n_components=3, perplexity=45, init="pca", learning_rate="auto", metric="cosine", random_state=0,
             max_iter=900).fit_transform(Z50)
    T = (T - T.mean(0)) / T.std(0)
    pos = {k: i for i, k in enumerate(ok_idx)}
    Tm = np.array([T[pos[k]] if k in pos else [np.nan] * 3 for k in prob_idx]) if len(prob_idx) else np.zeros((0, 3))
    fig = plt.figure(figsize=(7.1, 4.8), layout=None)
    y0 = stopka(fig)
    ax = fig.add_axes([0.0, y0, 1.0, 1.0 - y0 - .01], projection="3d", computed_zorder=False)
    ax.scatter(T[:, 0], T[:, 1], T[:, 2], s=6, c=KD[ok_idx], alpha=.8, linewidths=0, depthshade=True)
    gwiazdy(ax, Tm[:, 0], Tm[:, 1], Tm[:, 2], 85)
    bez_osi(ax)
    ax.set_box_aspect((1, 1, 1), zoom=1.12)
    ax.view_init(elev=18, azim=-55)
    zapisz(fig, "mapa_tsne3d")

    # ---------- 2. luki: ta sama chmura, kolor = odległość do najbliższej zrobionej próby (cosinus w 160 wymiarach)
    if len(prob_idx):
        Pn = e.vectors[prob_idx]
        Pn = Pn / np.linalg.norm(Pn, axis=1, keepdims=True)
        dist = 1 - (Vn @ Pn.T).max(axis=1)
        cm = LinearSegmentedColormap.from_list("luki", ["#1d2f5c", "#2a78b5", "#5fb8a8", "#e9d98a", "#f4f1e8"])
        fig = plt.figure(figsize=(7.1, 4.4), layout=None)
        ax = fig.add_axes([0.0, .13, .9, .86], projection="3d", computed_zorder=False)
        sc_ = ax.scatter(T[:, 0], T[:, 1], T[:, 2], s=6, c=dist, cmap=cm, alpha=.85, linewidths=0, depthshade=True,
                         vmin=0, vmax=np.percentile(dist, 98))
        gwiazdy(ax, Tm[:, 0], Tm[:, 1], Tm[:, 2], 85)
        bez_osi(ax)
        ax.set_box_aspect((1, 1, 1), zoom=1.12)
        ax.view_init(elev=18, azim=125)
        cax = fig.add_axes([.92, .3, .015, .45])
        cb = fig.colorbar(sc_, cax=cax)
        cb.set_label("odległość od najbliższej próby", fontsize=6.3)
        cb.set_ticks([0, np.percentile(dist, 98)])
        cb.set_ticklabels(["blisko", "daleko"])
        cb.ax.tick_params(labelsize=6)
        cb.outline.set_visible(False)
        h = [Line2D([], [], marker="*", ls="", mfc=KOL_WYNIK[k], mec="#111", ms=7, mew=.5, label=f"próba: {v}")
             for k, v in NAZ_WYNIK.items()]
        fig.legend(handles=h, loc="lower center", ncol=3, fontsize=6.5, frameon=False, bbox_to_anchor=(.45, .01))
        zapisz(fig, "luki3d")

    # ---------- 2b. teren gęstości (PC1 x PC2), przezroczysta powierzchnia, próby jako słupki z gwiazdką
    from scipy.stats import gaussian_kde
    from matplotlib.colors import LightSource
    P = (e.vectors - e.pca_mean) @ e.pca_basis
    P2 = P[ok_idx, :2]
    lo, hi = np.percentile(P2, 3, 0), np.percentile(P2, 97, 0)
    sel = np.all((P2 >= lo) & (P2 <= hi), axis=1)
    kde = gaussian_kde(P2[sel].T, bw_method=.32)
    gx, gy = np.meshgrid(np.linspace(lo[0], hi[0], 90), np.linspace(lo[1], hi[1], 90))
    gz = kde(np.vstack([gx.ravel(), gy.ravel()])).reshape(gx.shape)
    gzmax = gz.max()
    gz = (gz / gzmax) ** .55
    cmt = LinearSegmentedColormap.from_list("ter", ["#f4f1e8", "#cfe3d4", "#8fc1b5", "#4a98a8", "#2a5d8f", "#1d2f5c"])
    rgb = LightSource(azdeg=315, altdeg=40).shade(gz, cmap=cmt, vert_exag=.9, blend_mode="soft")
    rgb[..., 3] = .55
    fig = plt.figure(figsize=(7.1, 4.0), layout=None)
    y0 = stopka(fig, dzial=False)
    ax = fig.add_axes([0.0, y0, 1.0, 1.0 - y0], projection="3d", computed_zorder=False)
    ax.plot_surface(gx, gy, gz, facecolors=rgb, rstride=1, cstride=1, linewidth=0, antialiased=True, shade=False, zorder=1)
    ax.contour(gx, gy, gz, zdir="z", offset=-.02, levels=10, cmap=cmt, linewidths=.6, alpha=.9, zorder=0)
    gridv = kde(np.vstack([gx.ravel(), gy.ravel()]))
    for k, c in zip(prob_idx, prob_kol):
        x0, y0_ = np.clip(P[k, 0], lo[0], hi[0]), np.clip(P[k, 1], lo[1], hi[1])
        z0 = float((kde([[x0], [y0_]])[0] / gzmax) ** .55)
        ax.plot([x0, x0], [y0_, y0_], [z0, z0 + .2], color="#222", lw=.7, zorder=50)
        ax.scatter([x0], [y0_], [z0 + .2], s=55, marker="*", color=c, edgecolor="#111", linewidth=.5, depthshade=False, zorder=60)
    bez_osi(ax)
    ax.set_zlim(-.02, 1.3)
    ax.set_box_aspect((1.7, 1.4, .55), zoom=1.15)
    ax.view_init(elev=34, azim=-58)
    zapisz(fig, "teren3d")

    # ---------- 2c. kula: cała baza na sferze (kierunek wektora zadania), dwie półkule, przezroczysta kula
    U = np.array([m.project_160_to_3d(e.vectors[k]) for k in ok_idx])
    U = U / np.linalg.norm(U, axis=1, keepdims=True)
    Up = np.array([m.project_160_to_3d(e.vectors[k]) for k in prob_idx]) if len(prob_idx) else np.zeros((0, 3))
    if len(Up):
        Up = Up / np.linalg.norm(Up, axis=1, keepdims=True)
    th, ph = np.meshgrid(np.linspace(0, 2 * np.pi, 60), np.linspace(0, np.pi, 30))
    SX, SY, SZ = np.sin(ph) * np.cos(th), np.sin(ph) * np.sin(th), np.cos(ph)
    fig = plt.figure(figsize=(7.1, 3.8), layout=None)
    y0 = stopka(fig)
    for j, (az, el) in enumerate(((-60, 18), (120, 18))):
        ax = fig.add_axes([.5 * j, y0, .5, 1.0 - y0], projection="3d", computed_zorder=False)
        ax.plot_surface(SX, SY, SZ, color="#dfe6ee", alpha=.18, linewidth=0, shade=False, zorder=1)
        ax.plot_wireframe(SX, SY, SZ, color="#c9d1da", linewidth=.25, alpha=.5, rstride=3, cstride=3, zorder=2)
        v = np.array([np.cos(np.radians(el)) * np.cos(np.radians(az)), np.cos(np.radians(el)) * np.sin(np.radians(az)),
                      np.sin(np.radians(el))])
        vis = U @ v > -.02
        Uv, Kv = U[vis], KD[ok_idx][vis]
        o = np.argsort(Uv @ v)  # od najdalszych do najbliższych
        ax.scatter(*(1.01 * Uv[o]).T, s=4.5, c=Kv[o], alpha=.7, linewidths=0, depthshade=False, zorder=10)
        if len(Up):
            vp = Up @ v > .02
            ax.scatter(*(1.05 * Up[vp]).T, s=70, marker="*", c=np.array(prob_kol)[vp], edgecolor="#111", linewidth=.5,
                       depthshade=False, zorder=100)
        ax.set_xlim(-.8, .8); ax.set_ylim(-.8, .8); ax.set_zlim(-.8, .8)
        ax.set_box_aspect((1, 1, 1), zoom=1.2)
        ax.set_axis_off()
        ax.view_init(elev=el, azim=az)
    zapisz(fig, "sfera_bazy")

    # ---------- 3. grzbiety trudności
    from scipy.stats import gaussian_kde as gk
    xs = np.linspace(1.5, 6.5, 120)
    fig = plt.figure(figsize=(7.1, 4.0), layout=None)
    y0 = stopka(fig, dzial=False)
    ax = fig.add_axes([0.0, y0 + .04, .86, 1.0 - y0 - .04], projection="3d", computed_zorder=False)
    az_g, el_g = -62, 27
    ordg = od_tylu([(3.5, i) for i in range(len(dz_ord))], az_g, el_g, (1.7, 2.2))
    for rang, i in enumerate(ordg):
        d = dz_ord[i]
        v = A["norma"][(A["dzial"] == d) & A["maska"]]
        if len(v) < 4:
            continue
        y = gk(v, bw_method=.28)(xs) * len(v)
        verts = [(xs[0], 0)] + list(zip(xs, y)) + [(xs[-1], 0)]
        ax.add_collection3d(Poly3DCollection([[(a_, i, b_) for a_, b_ in verts]], facecolor=kol[d], edgecolor=kol[d],
                                             linewidth=.7, alpha=.42, zorder=rang + 1))
    for k, c in zip(prob_idx, prob_kol):
        d = A["dzial"][k]
        if d in dz_ord:
            ax.scatter([A["norma"][k]], [dz_ord.index(d)], [0], s=55, marker="*", color=c, edgecolor="#111", linewidth=.5,
                       depthshade=False, zorder=500)
    ax.set_xlim(1.5, 6.5)
    ax.set_ylim(-.5, len(dz_ord) - .5)
    ax.set_zlim(0, max(Counter(A["dzial"][A["maska"]]).values()) * .22)
    ax.set_yticks(range(len(dz_ord)), [_skrot_raport_atlas(d, 20) for d in dz_ord], fontsize=5.4, ha="left", va="center")
    ax.set_xticks([2, 3, 4, 5, 6])
    ax.tick_params(axis="x", labelsize=6, pad=-3)
    ax.tick_params(axis="y", pad=-1)
    ax.tick_params(axis="z", labelsize=5.5, pad=-1)
    ax.set_xlabel("trudność zadania (norma wektora)", fontsize=6.3, labelpad=-4)
    ax.set_zlabel("liczba zadań", fontsize=6.3, labelpad=-6)
    panele(ax)
    ax.set_box_aspect((1.7, 2.2, .75), zoom=1.0)
    ax.view_init(elev=el_g, azim=az_g)
    zapisz(fig, "grzbiety3d")

    # ---------- 4. dział x rok arkusza (słupki 3D), gwiazdki nad słupkami z próbą
    lata = sorted({r for r in A["rok"][A["maska"]] if r})
    if lata:
        fig = plt.figure(figsize=(7.1, 4.0), layout=None)
        y0 = stopka(fig, dzial=False)
        ax = fig.add_axes([0.0, y0 + .04, .86, 1.0 - y0 - .04], projection="3d", computed_zorder=False)
        az_r, el_r = -52, 46
        kom = [(j, i) for i in range(len(dz_ord)) for j in range(len(lata))]
        for rang, idx_ in enumerate(od_tylu(kom, az_r, el_r, (2.0, 2.0))):
            j, i = kom[idx_]
            n_ = int(np.sum((A["dzial"] == dz_ord[i]) & (A["rok"] == lata[j]) & A["maska"]))
            if n_:
                ax.bar3d(j - .32, i - .32, 0, .64, .64, n_, color=kol[dz_ord[i]], alpha=.45, shade=True,
                         edgecolor=kol[dz_ord[i]], linewidth=.25, zorder=rang + 1)
        zmax = 0
        for k, c in zip(prob_idx, prob_kol):
            d, r_ = A["dzial"][k], A["rok"][k]
            if d in dz_ord and r_ in lata:
                n_ = int(np.sum((A["dzial"] == d) & (A["rok"] == r_) & A["maska"]))
                ax.scatter([lata.index(r_)], [dz_ord.index(d)], [n_ + 5], s=55, marker="*", color=c, edgecolor="#111",
                           linewidth=.5, depthshade=False, zorder=5000)
        ax.set_xticks(range(0, len(lata), 3), [str(r_) for r_ in lata[::3]])
        ax.set_yticks(range(len(dz_ord)), [_skrot_raport_atlas(d, 20) for d in dz_ord], fontsize=5.4, ha="left", va="center")
        ax.tick_params(axis="x", labelsize=6, pad=-3)
        ax.tick_params(axis="y", pad=-1)
        ax.tick_params(axis="z", labelsize=5.5, pad=-1)
        ax.set_xlabel("rok arkusza CKE", fontsize=6.3, labelpad=-4)
        ax.set_zlabel("liczba zadań", fontsize=6.3, labelpad=-6)
        panele(ax)
        ax.set_box_aspect((2.0, 2.0, .55), zoom=1.08)
        ax.view_init(elev=el_r, azim=az_r)
        zapisz(fig, "dzial_rok3d")
    return out, dz_ord, kol


BIB = r"""\begin{thebibliography}{9}\small
\bibitem{tsne} L.~van der Maaten, G.~Hinton. Visualizing data using t-SNE. \emph{J.\ Mach.\ Learn.\ Res.}, 9:2579--2605, 2008.
\bibitem{cke} Centralna Komisja Egzaminacyjna. Arkusze egzaminu maturalnego z matematyki, poziom rozszerzony, 2002--2026.
\end{thebibliography}
"""


def zloz_atlas(D, wyk, dz_ord, symulacja):
    A, k = D["A"], D["kpi"]
    ost = A["ost"]
    n_baza = int(A["maska"].sum())
    L = [PREAMBULA.replace("@@NAGLOWEK@@", f"atlas bazy zadań, {D['dzis']:%d.%m.%Y}")]
    P = L.append
    P(r"\thispagestyle{plain}\begin{center}{\LARGE\bfseries Atlas bazy zadań maturalnych:\\[2pt] wszystkie " + str(n_baza)
      + r" zadania w trzech wymiarach\par}\vspace{8pt}{\large uczeń\textsuperscript{1} \qquad Claude\textsuperscript{2}\par}\vspace{3pt}"
      r"{\small\textsuperscript{1}Uczeń, matura 2027 \qquad \textsuperscript{2}Korepetytor, system \texttt{matura.db}\par}\vspace{3pt}"
      r"{\small " + _tex_raport_atlas(_data_pl(D["dzis"])) + r"\par}")
    if symulacja:
        P(r"\vspace{4pt}{\small\bfseries\color{zle}RAPORT TESTOWY NA DANYCH SYMULOWANYCH: gwiazdki nie są próbami ucznia.\par}")
    P(r"\end{center}")
    P(r"\begin{abstract}\noindent Baza \texttt{matura.db} zawiera " + str(n_baza) + r" zadań z arkuszy CKE~\cite{cke} z lat "
      + f"{min(r for r in A['rok'] if r)}--{max(A['rok'])}, każde opisane wektorem o 160 współrzędnych. Pokazujemy ją jako mapę (t-SNE~\\cite{{tsne}}), "
      r"mapę zasięgu prób, grzbiety trudności i zestawienie po latach. Na każdej mapie uczeń jest zaznaczony gwiazdkami: "
      + f"{len(ost)} zadań z {n_baza}, czyli {100 * len(ost) / n_baza:.1f}\\%. Mapa pokazuje, gdzie leży cały materiał, a gdzie jeszcze nie byłeś."
      r"\end{abstract}")
    P(r"\section{Mapa całej bazy}")
    P(r"Zadanie w bazie ma wektor z 160 współrzędnych: opisuje, jakich umiejętności wymaga i jak jest trudne; jego długość (norma) służy jako miara trudności. "
      r"Rys.~\ref{fig:tsne} rzutuje wszystkie wektory do trzech wymiarów metodą t-SNE~\cite{tsne}: zadania podobne trafiają obok siebie, a kolor to dział "
      r"(taki sam na wszystkich rysunkach). Gwiazdki to zadania ucznia: zielona komplet, pomarańczowa częściowo, czerwona zero.")
    P(_rys(wyk["mapa_tsne3d"], "Cała baza w t-SNE 3D: punkt to zadanie, kolor to dział, gwiazdka to próba ucznia.", "fig:tsne"))
    if "luki3d" in wyk:
        P(r"Rys.~\ref{fig:luki} to ta sama chmura (inny kąt patrzenia), ale pokolorowana odległością od najbliższej zadanej próby, liczoną w pełnych 160 wymiarach: "
          r"ciemne punkty leżą blisko tego, co uczeń już robił, jasne to rejony nietknięte.")
        P(_rys(wyk["luki3d"], "Zasięg dotychczasowych prób: ciemne zadania są blisko jakiejś próby, jasne daleko.", "fig:luki"))
    P(r"\section{Teren i kula}")
    P(r"Rys.~\ref{fig:teren} pokazuje gęstość zadań na dwóch głównych osiach PCA: wzgórza to typy zadań, których jest dużo, doliny to rzadkie. "
      r"Słupki z gwiazdkami stoją tam, gdzie uczeń już był. Rys.~\ref{fig:sfera} to cała baza na kuli (kierunek wektora zadania), "
      r"tak jak liczymy sferę kompetencji; lewa i prawa połowa to dwie przeciwne półkule.")
    P(_rys(wyk["teren3d"], "Teren bazy: przezroczysta powierzchnia gęstości zadań na osiach PC1 i PC2, gwiazdki to próby ucznia.", "fig:teren"))
    P(_rys(wyk["sfera_bazy"], "Baza na przezroczystej kuli, dwie półkule; kolory jak na mapie, gwiazdki to próby ucznia.", "fig:sfera"))
    P(r"\section{Trudność i lata}")
    P(r"Rys.~\ref{fig:grzbiety} pokazuje rozkład trudności zadań w każdym dziale: wstęga jest wyższa tam, gdzie zadań o danej trudności jest więcej. "
      r"Rys.~\ref{fig:rok} zestawia liczbę zadań z działem i rokiem arkusza. Dokładne liczby zadań w działach podaje Tab.~\ref{tab:pokrycie}.")
    P(_rys(wyk["grzbiety3d"], "Grzbiety trudności: rozkład normy wektora zadań w każdym dziale (wysokość to liczba zadań); gwiazdka to próba ucznia.", "fig:grzbiety"))
    if "dzial_rok3d" in wyk:
        P(_rys(wyk["dzial_rok3d"], "Liczba zadań według działu i roku arkusza CKE; gwiazdka nad słupkiem to próba ucznia.", "fig:rok"))
    P(r"\section{Gdzie uczeń już był}")
    cnt = Counter(A["dzial"][A["maska"]])
    tk = Counter(A["dzial"][A["idx"][i]] for i in ost)
    P(r"\begin{table}[!htbp]\centering\caption{Pokrycie bazy po działach.}\label{tab:pokrycie}\small\begin{tabular}{@{}lrrrr@{}}\toprule Dział & W bazie & Zrobione & Pokrycie (\%) & Śr. wynik (\%)\\\midrule")
    for d in sorted(cnt, key=lambda d: -cnt[d]):
        w = [x["sc"] * 10 for x in ost.values() if x["dzial"] == d]
        P(f"{_tex_raport_atlas(d)} & {cnt[d]} & {tk[d]} & {100 * tk[d] / cnt[d]:.1f} & {(f'{sum(w) / len(w):.0f}' if w else '---')}\\\\")
    P(r"\bottomrule\end{tabular}\end{table}")
    P(f"Razem {len(ost)} zadań z {n_baza} ({100 * len(ost) / n_baza:.1f}\\%). Przy tak małym pokryciu mapa mówi głównie, "
      r"\emph{ile jest jeszcze do zobaczenia}, a nie, jak uczeń radzi sobie w poszczególnych działach; oceny z modelu opanowania "
      r"są w raporcie z całej historii.")
    P(r"\paragraph{Czego nie widać.} Wektory pochodzą w części z OCR-u tekstu zadań, więc położenie pojedynczego zadania na mapie bywa zaszumione; "
      r"t-SNE zachowuje sąsiedztwo, a nie odległości, więc rozmiaru skupisk nie wolno czytać dosłownie. "
      r"Zadań o zerowym wektorze nie ma na mapach.")
    P(BIB)
    P(r"\paragraph{Dostępność kodu.} Raport wygenerowano " + f"{_dt_raport_atlas(time.time()):%d.%m.%Y %H:%M}" + r" skryptem \texttt{raport\_atlas.py} (tylko odczyt bazy).")
    P(r"\end{document}")
    return "\n".join(L)


def _main_raport_atlas(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dzien")
    ap.add_argument("--symulacja", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    zapewnij_zaleznosci()
    if subprocess.run([sys.executable, "-c", "import sklearn"], capture_output=True).returncode:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "scikit-learn"], check=True)
    dzien = date.fromisoformat(a.dzien) if a.dzien else datetime.now(TZ).date()
    teraz = time.time() if not a.dzien else datetime(dzien.year, dzien.month, dzien.day, 9, 47, tzinfo=TZ).timestamp()
    if a.symulacja:
        dzien = date(2026, 11, 21)
        teraz = datetime(2026, 11, 21, 9, 47, tzinfo=TZ).timestamp()
        kat_bazy = przygotuj_symulacje(dzien)
    else:
        kat_bazy = BASE
    import matura as m
    p = m
    assert Path(m.__file__).resolve().parent == kat_bazy.resolve(), "zły katalog modułów"
    out = Path(a.out) if a.out else RAPORTY / (dzien.isoformat() + ("_atlas_sym" if a.symulacja else "_atlas"))
    out.mkdir(parents=True, exist_ok=True)
    D = zbierz_atlas(m, p, teraz)
    wyk, dz_ord, _ = wykresy_atlas(D, out / "wykresy")
    (out / "raport.tex").write_text(zloz_atlas(D, wyk, dz_ord, a.symulacja), encoding="utf-8")
    for _ in range(2):
        r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "raport.tex"], cwd=out,
                           capture_output=True, text=True, errors="replace")
        if r.returncode:
            print(r.stdout[-3000:])
            raise SystemExit("[atlas] BŁĄD pdflatex")
    for f in ("raport.aux", "raport.out"):
        (out / f).unlink(missing_ok=True)
    pod = out / "podglad"
    shutil.rmtree(pod, ignore_errors=True)
    pod.mkdir()
    if shutil.which("pdftoppm"):
        subprocess.run(["pdftoppm", "-png", "-r", "80", str(out / "raport.pdf"), str(pod / "strona")], check=False)
    (out / "dane.json").write_text(json.dumps({"dzien": dzien.isoformat(), "wykresy": sorted(wyk), "proby": len(D["A"]["ost"])},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
    if a.symulacja:
        shutil.rmtree(kat_bazy, ignore_errors=True)
    print(f"[atlas] OK: {out / 'raport.pdf'}")


# ==============================================================================
# SEKCJA: raport_calosc (dawniej raport_calosc.py)
# ==============================================================================

# Raport z całej bazy danych ucznia (cała historia prób, nie jeden tydzień): artykuł w LaTeX-u z wykresami 2D i 3D.
#
# Uruchomienie (ręcznie, na prośbę ucznia; nie jest zaplanowane):
#     python3 raport_calosc.py
#     python3 raport_calosc.py --komentarz K.md
#     python3 raport_calosc.py --symulacja       # test na symulowanym uczniu (kopia bazy w /tmp)
#
# Wynik: raporty/RRRR-MM-DD_calosc/ (symulacja: RRRR-MM-DD_calosc_sym/) z raport.pdf, raport.tex, dane.json,
# wykresy/*.pdf, podglad/strona-N.png.
#
# Zbudowany na matura.py raport (importuje zbierz, wykresy i preambułę; matura.py raport nie jest zmieniany). Wszystkie wykresy
# z matura.py raport liczą się po całej historii, tu dochodzą wykresy rozbudowane: oś prób, sesje, pokrycie bazy, macierze
# dział x sesja i błąd x sesja, narastające punkty, 3D prób. Tylko czyta bazę. Gdzie danych brakuje, raport mówi to wprost.



_dt_raport_calosc, _tex_raport_calosc, _skrot_raport_calosc = _dt, _tex, _skrot
OI = {"nieb": "#0072B2", "pom": "#E69F00", "ziel": "#009E73", "czer": "#D55E00", "jasn": "#56B4E9",
      "fiol": "#CC79A7", "szary": "#7A7A7A", "ciem": "#222222"}
KOL_WYNIK_raport_calosc = {"ok": OI["ziel"], "partial": OI["pom"], "fail": OI["czer"]}
NAZWA_WYNIK = {"ok": "komplet", "partial": "częściowo", "fail": "zero"}
PRZERWA_SESJI = 2 * 3600  # próby oddzielone o więcej niż 2 h to osobne sesje


def sesje(proby):
    """Przypisuje każdej próbie numer sesji (0, 1, ...) wg przerw w czasie zapisu."""
    s, ost, n = [], None, -1
    for x in sorted(proby, key=lambda x: x["ts"]):
        if ost is None or x["ts"] - ost > PRZERWA_SESJI:
            n += 1
        ost = x["ts"]
        s.append(n)
        x["sesja"] = n
    return n + 1


def rok_zadania(nazwa: str):
    mt = re.match(r"CKE_(\d{4})", nazwa or "")
    return int(mt.group(1)) if mt else None


def zbierz_calosc(m, p, teraz):
    D = zbierz(m, p, teraz)
    proby = D["proby"]
    ns = sesje(proby)
    D["n_sesji"] = ns
    # pokrycie bazy po działach i po latach
    e = D["engine"]
    con_n = {}
    import sqlite3
    con = sqlite3.connect(Path(m.DB))
    nazwy = dict(con.execute("SELECT id, name FROM desc").fetchall())
    con.close()
    baza_dz, baza_rok = Counter(), Counter()
    for tid in e.ids.tolist():
        baza_dz[p.Dzial(int(tid))] += 1
        r = rok_zadania(nazwy.get(int(tid), ""))
        if r:
            baza_rok[r] += 1
    D["baza_dz"], D["baza_rok"], D["baza_n"] = baza_dz, baza_rok, int(len(e.ids))
    D["proby_rok"] = Counter(rok_zadania(x["nazwa"]) for x in {x["id"]: x for x in proby}.values())
    return D


# ============================================================================ wykresy rozszerzone

def wykresy_calosc(D, kat: Path) -> dict:
    import numpy as np
    plt = _styl()
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.lines import Line2D
    import matplotlib.dates as mdates
    kat.mkdir(parents=True, exist_ok=True)
    out = {}
    div = LinearSegmentedColormap.from_list("div", [CZERWONY, "#f0efec", NIEBIESKI])
    proby = sorted(D["proby"], key=lambda x: x["ts"])

    def zapisz(fig, nazwa):
        for ax in fig.axes:
            ax.set_title("")
        fig.savefig(kat / f"{nazwa}.pdf")
        plt.close(fig)
        out[nazwa] = f"wykresy/{nazwa}.pdf"

    def osie(ax, siatka="y"):
        ax.grid(False)
        if siatka:
            ax.grid(axis=siatka, color="#E6E6E6", lw=.5, zorder=0)
        ax.set_axisbelow(True)
        for s_ in ("left", "bottom"):
            ax.spines[s_].set_color("#333333")
            ax.spines[s_].set_linewidth(.7)
        ax.tick_params(colors="#333333", direction="out", length=2.5, width=.6)

    if not proby:
        return out
    ns = D["n_sesji"]
    sc = np.array([x["sc"] * 10 for x in proby])
    n = len(proby)

    # 1. oś prób: lizak na próbę (kolor = ocena), średnia narastająca, granice sesji z datą
    fig, ax = plt.subplots(figsize=(7.1, 2.5))
    idx = np.arange(1, n + 1)
    ax.vlines(idx, 0, sc, color="#CFCFCF", lw=.9, zorder=2)
    ax.scatter(idx, sc, s=22, c=[KOL_WYNIK_raport_calosc[x["wynik"]] for x in proby], edgecolor="white", linewidth=.5, zorder=4)
    nar = np.cumsum(sc) / idx
    ax.plot(idx, nar, color=OI["nieb"], lw=1.2, zorder=3)
    ax.annotate(f"średnia narastająca: {nar[-1]:.0f}%", (idx[-1], nar[-1]), xytext=(-4, 7), textcoords="offset points",
                ha="right", fontsize=6.5, color=OI["nieb"])
    for s_ in range(ns):
        grupa = [i + 1 for i, x in enumerate(proby) if x["sesja"] == s_]
        if s_ > 0:
            ax.axvline(grupa[0] - .5, color=OI["szary"], lw=.6, ls=(0, (4, 3)), zorder=1)
        ax.annotate(f"{_dt_raport_calosc(proby[grupa[0] - 1]['ts']):%d.%m %H:%M}", (np.mean(grupa), 1.0), xycoords=("data", "axes fraction"),
                    xytext=(0, 2), textcoords="offset points", ha="center", va="bottom", fontsize=6, color=OI["szary"])
    for prog in (60, 75, 90):
        ax.axhline(prog, color="#E3E3E3", lw=.6, zorder=0)
        ax.annotate(f"{prog}%", (1, prog), xycoords=("axes fraction", "data"), xytext=(3, 0), textcoords="offset points",
                    va="center", fontsize=6, color=OI["szary"])
    ax.set_ylim(-3, 105)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_xlim(.4, n + .6)
    ax.xaxis.get_major_locator().set_params(integer=True)
    ax.set_xlabel("kolejna próba (od najstarszej)")
    ax.set_ylabel("wynik zadania (%)")
    osie(ax)
    fig.legend(handles=[Line2D([], [], marker="o", ls="", color=KOL_WYNIK_raport_calosc[k], ms=4.5, label=v) for k, v in NAZWA_WYNIK.items()]
               + [Line2D([], [], color=OI["nieb"], lw=1.2, label="średnia narastająca")],
               loc="outside lower center", ncol=4, fontsize=6.5)
    zapisz(fig, "os_prob")

    # 2. sesje: średnia sesji (romb) z rozrzutem prób (kółka), liczba prób przy sesji
    fig, ax = plt.subplots(figsize=(3.45, 2.3))
    rng = np.random.default_rng(2)
    for s_ in range(ns):
        v = np.array([x["sc"] * 10 for x in proby if x["sesja"] == s_])
        ax.vlines(s_, v.min(), v.max(), color="#CFCFCF", lw=1.2, zorder=1)
        ax.scatter(s_ + rng.uniform(-.08, .08, len(v)), v, s=11, facecolor="white", edgecolor=OI["szary"], linewidth=.6, zorder=3)
        ax.scatter([s_], [v.mean()], s=36, marker="D", color=OI["nieb"], zorder=4)
        ax.annotate(f"{v.mean():.0f}% (n={len(v)})", (s_, v.mean()), xytext=(7, 0), textcoords="offset points",
                    va="center", fontsize=6.3, color=OI["nieb"])
    ax.set_xticks(range(ns), [f"{_dt_raport_calosc(min(x['ts'] for x in proby if x['sesja'] == s_)):%d.%m\n%H:%M}" for s_ in range(ns)])
    ax.set_xlim(-.5, ns - .2)
    ax.set_ylim(-3, 105)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_xlabel("sesja sprawdzania")
    ax.set_ylabel("wynik zadania (%)")
    osie(ax)
    zapisz(fig, "sesje")

    # 3. punkty narastająco: zdobyte i możliwe (tylko próby z notatką o punktach)
    pp = [x for x in proby if x["maks"]]
    if len(pp) >= 2:
        fig, ax = plt.subplots(figsize=(3.45, 2.3))
        xs = np.arange(1, len(pp) + 1)
        zd, mo = np.cumsum([x["pkt"] for x in pp]), np.cumsum([x["maks"] for x in pp])
        ax.fill_between(xs, zd, mo, color=OI["czer"], alpha=.10, lw=0, zorder=1)
        ax.plot(xs, mo, color=OI["szary"], lw=.9, ls=(0, (4, 3)), zorder=2)
        ax.plot(xs, zd, color=OI["nieb"], lw=1.3, zorder=3)
        ax.scatter(xs, zd, s=10, color=OI["nieb"], zorder=4)
        ax.annotate(f"zdobyte {zd[-1]:g}", (xs[-1], zd[-1]), xytext=(-3, -9), textcoords="offset points", ha="right",
                    fontsize=6.3, color=OI["nieb"])
        ax.annotate(f"możliwe {mo[-1]:g}", (xs[-1], mo[-1]), xytext=(-3, 4), textcoords="offset points", ha="right",
                    fontsize=6.3, color=OI["szary"])
        ax.set_xlabel("kolejna próba z punktami")
        ax.set_ylabel("punkty narastająco")
        ax.xaxis.get_major_locator().set_params(integer=True)
        osie(ax)
        zapisz(fig, "punkty_narastajaco")

    # 4. dział x sesja: macierz kropek (wielkość = liczba prób, kolor = średni wynik)
    dz = sorted({x["dzial"] for x in proby}, key=lambda d: np.mean([x["sc"] for x in proby if x["dzial"] == d]))
    fig, ax = plt.subplots(figsize=(3.45, 0.24 * len(dz) + 1.0))
    ax.scatter(*np.meshgrid(range(ns), range(len(dz))), s=4, color="#DDDDDD", lw=0, zorder=1)
    for i_, d in enumerate(dz):
        for s_ in range(ns):
            v = [x["sc"] for x in proby if x["dzial"] == d and x["sesja"] == s_]
            if v:
                ax.scatter(s_, i_, s=60 + 70 * len(v), color=div(np.mean(v)), edgecolor=OI["ciem"], linewidth=.4, zorder=3)
                ax.text(s_, i_, f"{10 * np.mean(v):.0f}", ha="center", va="center", fontsize=5.8, color=OI["ciem"], zorder=4)
        ax.annotate(f"{10 * np.mean([x['sc'] for x in proby if x['dzial'] == d]):.0f}%", (1, i_), xycoords=("axes fraction", "data"),
                    xytext=(4, 0), textcoords="offset points", va="center", fontsize=6.3, color=OI["ciem"])
    ax.annotate("razem", (1, len(dz) - .4), xycoords=("axes fraction", "data"), xytext=(4, 2), textcoords="offset points",
                va="bottom", fontsize=6, color=OI["szary"])
    ax.set_yticks(range(len(dz)), [_skrot_raport_calosc(d) for d in dz])
    ax.set_xticks(range(ns), [f"{_dt_raport_calosc(min(x['ts'] for x in proby if x['sesja'] == s_)):%d.%m}" for s_ in range(ns)])
    ax.set_xlim(-.6, ns - .4)
    ax.set_ylim(-.6, len(dz) - .4)
    ax.set_xlabel("sesja (liczba w kropce: wynik %)")
    ax.grid(True, color="#ECECEC", lw=.5, zorder=0)
    for s_ in ax.spines.values():
        s_.set_visible(False)
    ax.tick_params(length=0, colors="#333333")
    zapisz(fig, "dzial_sesja")

    # 5. błąd x sesja: typy błędów w kolejnych sesjach
    bl = [x for x in proby if x["typ"] and x["wynik"] != "ok"]
    if bl:
        ty = [t_ for t_ in TYPY if any(x["typ"] == t_ for x in bl)]
        fig, ax = plt.subplots(figsize=(3.45, 0.26 * len(ty) + 1.0))
        for i_, t_ in enumerate(ty):
            for s_ in range(ns):
                v = sum(1 for x in bl if x["typ"] == t_ and x["sesja"] == s_)
                if v:
                    ax.scatter(s_, i_, s=50 + 60 * v, color=OI["nieb"], alpha=.9, lw=0, zorder=3)
                    ax.text(s_, i_, str(v), ha="center", va="center", fontsize=5.8, color="white", zorder=4)
        ax.set_yticks(range(len(ty)), ty)
        ax.set_xticks(range(ns), [f"{_dt_raport_calosc(min(x['ts'] for x in proby if x['sesja'] == s_)):%d.%m}" for s_ in range(ns)])
        ax.set_xlim(-.6, ns - .4)
        ax.set_ylim(-.6, len(ty) - .4)
        ax.set_xlabel("sesja (liczba błędów danego typu)")
        ax.grid(True, color="#ECECEC", lw=.5, zorder=0)
        for s_ in ax.spines.values():
            s_.set_visible(False)
        ax.tick_params(length=0, colors="#333333")
        zapisz(fig, "blad_sesja")

    # 6. pokrycie bazy po działach: ułamek zadań dotkniętych (lizak) z liczbami
    bd = D["baza_dz"]
    tk = Counter()
    for x in {x["id"]: x for x in proby}.values():
        tk[x["dzial"]] += 1
    dzs = sorted(bd, key=lambda d: (tk[d] / bd[d], bd[d]))
    fig, ax = plt.subplots(figsize=(3.45, 0.22 * len(dzs) + 0.9))
    fr = [100 * tk[d] / bd[d] for d in dzs]
    ax.hlines(range(len(dzs)), 0, fr, color="#CFCFCF", lw=1, zorder=1)
    ax.scatter(fr, range(len(dzs)), s=22, color=[OI["nieb"] if tk[d] else OI["szary"] for d in dzs], zorder=3)
    for i_, d in enumerate(dzs):
        ax.annotate(f"{tk[d]} z {bd[d]}", (fr[i_], i_), xytext=(5, 0), textcoords="offset points", va="center",
                    fontsize=6.3, color=OI["ciem"])
    ax.set_yticks(range(len(dzs)), [_skrot_raport_calosc(d) for d in dzs])
    ax.set_ylim(-.6, len(dzs) - .4)
    ax.set_xlim(0, max(max(fr) * 1.5, 5))
    ax.set_xlabel("zadania zrobione / zadania działu w bazie (%)")
    osie(ax, "x")
    zapisz(fig, "pokrycie_dzialy")

    # 7. baza po latach CKE: liczba zadań w bazie (szary) i zrobionych (niebieski)
    br = D["baza_rok"]
    if br:
        lata = sorted(br)
        fig, ax = plt.subplots(figsize=(3.45, 2.3))
        ax.vlines(lata, 0, [br[r] for r in lata], color="#CFCFCF", lw=1.2, zorder=1)
        ax.scatter(lata, [br[r] for r in lata], s=14, color=OI["szary"], zorder=3, label="w bazie")
        pr = [(r, D["proby_rok"].get(r, 0)) for r in lata if D["proby_rok"].get(r, 0)]
        if pr:
            ax.scatter([r for r, _ in pr], [v for _, v in pr], s=22, marker="D", color=OI["nieb"], zorder=4, label="zrobione")
        ax.set_xlabel("rok arkusza CKE")
        ax.set_ylabel("liczba zadań")
        ax.set_xticks(lata[::3])
        osie(ax)
        ax.legend(loc="upper left", fontsize=6.3)
        zapisz(fig, "pokrycie_lata")

    # 8. 3D: próby jako lizaki w przestrzeni nr próby x dział x wynik
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    dz3 = sorted({x["dzial"] for x in proby}, key=lambda d: np.mean([x["sc"] for x in proby if x["dzial"] == d]))
    fig = plt.figure(figsize=(7.1, 3.0), layout=None)
    ax = fig.add_axes([0.0, -0.08, 0.96, 1.15], projection="3d", computed_zorder=False)
    for i, x in enumerate(proby):
        j = dz3.index(x["dzial"])
        z = x["sc"] * 10
        ax.plot([i + 1, i + 1], [j, j], [0, z], color="#BDBDBD", lw=.7, zorder=1)
        ax.scatter([i + 1], [j], [z], s=22, color=KOL_WYNIK_raport_calosc[x["wynik"]], edgecolor="white", linewidth=.4, depthshade=False, zorder=3)
    ax.set_yticks(range(len(dz3)), [_skrot_raport_calosc(d, 20) for d in dz3], fontsize=5.0, ha="left", va="center")
    ax.set_xticks(list(range(1, n + 1, max(1, n // 9))))
    ax.tick_params(axis="x", labelsize=5.3)
    ax.set_zlim(0, 100)
    ax.set_zticks([0, 25, 50, 75, 100], ["0", "25", "50", "75", "100%"], fontsize=5.5)
    ax.set_xlabel("kolejna próba", fontsize=6, labelpad=-4)
    ax.tick_params(axis="x", pad=-4)
    ax.tick_params(axis="y", pad=-3)
    ax.tick_params(axis="z", pad=-1)
    ax.view_init(elev=24, azim=-62)
    ax.set_box_aspect((2.0, 2.2, .8), zoom=1.05)
    for a in (ax.xaxis, ax.yaxis, ax.zaxis):
        a.pane.set_facecolor("#f7f7f5")
        a.pane.set_edgecolor(GRID)
    zapisz(fig, "proby3d")
    return out


# ============================================================================ skład

def zloz_calosc(D, wyk: dict, wyk2: dict, komentarz: str | None, symulacja: bool) -> str:
    k, prof, proby = D["kpi"], D["profil"], sorted(D["proby"], key=lambda x: x["ts"])
    ok_prof = prof.get("status") == "ok"
    od, do = (_dt_raport_calosc(proby[0]["ts"]).date(), D["dzis"]) if proby else (D["dzis"], D["dzis"])
    pre = PREAMBULA.replace("@@NAGLOWEK@@", f"cała baza, {od:%d.%m}–{do:%d.%m.%Y}")
    pre = pre.replace(r"\usepackage{microtype,", r"\usepackage{longtable,microtype,")
    L = [pre]
    A = L.append
    ns = D["n_sesji"]
    A(r"\thispagestyle{plain}\begin{center}")
    A(r"{\LARGE\bfseries Modelowanie opanowania materiału maturalnego\\[2pt] z matematyki rozszerzonej: raport z całej bazy danych\par}\vspace{8pt}")
    A(r"{\large uczeń\textsuperscript{1} \qquad Claude\textsuperscript{2}\par}\vspace{3pt}")
    A(r"{\small\textsuperscript{1}Uczeń, matura 2027 \qquad \textsuperscript{2}Korepetytor, system \texttt{matura.db}\par}\vspace{3pt}")
    A(r"{\small " + _tex_raport_calosc(_data_pl(D["dzis"])) + rf" \textbullet{{}} cała historia: {od:%d.%m}–{do:%d.%m.%Y}"
      + r" \textbullet{} faza: " + _tex_raport_calosc(D["tydz"]["faza"]) + r"\par}")
    if symulacja:
        A(r"\vspace{4pt}{\small\bfseries\color{zle}RAPORT TESTOWY NA DANYCH SYMULOWANYCH: to nie są wyniki ucznia.\par}")
    A(r"\end{center}\vspace{-2pt}")

    ab = [f"Raport obejmuje całą dotychczasową historię: {k['lacznie']} sprawdzonych prób z {k['zadan']} różnych zadań CKE~\\cite{{cke}} "
          f"w {ns} sesjach sprawdzania i {k['dni_akt'] if False else len({_dt(x['ts']).date() for x in proby})} dniach. "
          + (f"Średni wynik zadania to {k['sr_calosc']:g}\\%. " if k["sr_calosc"] is not None else "")
          + f"Ruszone jest {100 * k['zadan'] / D['baza_n']:.1f}\\% z {D['baza_n']} zadań bazy, więc wnioski o działach opierają się "
            "na bardzo małej liczbie prób i mają szerokie przedziały niepewności."]
    if ok_prof and prof["slabe"]:
        ab.append("Najsłabsze działy: " + _tex_raport_calosc(", ".join(f"{x['dzial']} ({x['opanowanie']}%)" for x in prof["slabe"][:3])) + ".")
    if ok_prof and prof["mocne"]:
        ab.append("Najmocniejsze: " + _tex_raport_calosc(", ".join(f"{x['dzial']} ({x['opanowanie']}%)" for x in prof["mocne"][:3])) + ".")
    A(r"\begin{abstract}\noindent " + " ".join(ab) + r"\par\smallskip\noindent\textbf{Słowa kluczowe:} "
      r"knowledge tracing, regresja logistyczna, przybliżenie Laplace'a, harmoniki sferyczne, pokrycie bazy zadań.\end{abstract}")

    # ---- 1. wstęp
    A(r"\section{Wstęp}")
    A(r"Raport tygodniowy opisuje jeden tydzień; ten opisuje wszystko, co baza wie o uczniu od pierwszej sprawdzonej próby. "
      r"Sekcja~\ref{sec:dane} omawia dane i ich braki, sekcja~\ref{sec:metody} metody, sekcja~\ref{sec:wyniki} wyniki: oś prób, sesje, "
      r"profil działów, pokrycie bazy, błędy i reprezentacje trójwymiarowe. Sekcja~\ref{sec:braki} wylicza, czego na razie nie da się "
      r"policzyć. Raport jest zbudowany tak, żeby ten sam skrypt dawał pełniejszy obraz w miarę napływu prób.")

    # ---- 2. dane
    A(r"\section{Dane}\label{sec:dane}")
    A(r"Źródłem jest tabela \texttt{history}: każda próba ma identyfikator zadania, ocenę z punktów, typ błędu i czas zapisu. "
      r"Czas zapisu to chwila sprawdzenia rozwiązania, a nie czas jego rozwiązywania; dlatego próby sprawdzone razem tworzą jedną sesję "
      + f"(przerwa dłuższa niż 2 godziny rozdziela sesje; wyszło {ns}). Tabela~\\ref{{tab:ogol}} streszcza całość.")
    pk_all = [x for x in proby if x["maks"]]
    A(r"\begin{table}[!htbp]\centering\caption{Statystyki całej historii.}\label{tab:ogol}\small\begin{tabular}{@{}lr@{}}\toprule Miara & Wartość\\\midrule")
    A(f"Sprawdzone próby & {k['lacznie']}\\\\")
    A(f"Różne zadania & {k['zadan']}\\\\")
    A(f"Zadania w bazie & {D['baza_n']}\\\\")
    A(f"Sesje sprawdzania & {ns}\\\\")
    A(f"Dni z próbami & {len({_dt(x['ts']).date() for x in proby})}\\\\")
    A(f"Średni wynik zadania (\\%) & {_kpi_val(k['sr_calosc'], '{:g}')}\\\\")
    A(f"Komplet / częściowo / zero & {sum(x['wynik']=='ok' for x in proby)} / {sum(x['wynik']=='partial' for x in proby)} / {sum(x['wynik']=='fail' for x in proby)}\\\\")
    if pk_all:
        A(f"Punkty z zadań & {sum(x['pkt'] for x in pk_all):g} z {sum(x['maks'] for x in pk_all)} ({len(pk_all)} prób z punktami)\\\\")
    A(f"Średnie opanowanie z modelu (\\%) & {_kpi_val(k['gotowosc'])}\\\\")
    A(f"Pełne arkusze CKE & {len(D['pelne'])}\\\\")
    A(f"Dni do matury & {k['dni_do_matury']}\\\\\\bottomrule\\end{{tabular}}\\end{{table}}")

    # ---- 3. metody
    A(r"\section{Metody}\label{sec:metody}")
    A(r"\paragraph{Model opanowania.} Każda próba to wynik ułamkowy (punkty zdobyte przez maksymalne). Szansę na komplet punktów modelujemy "
      r"regresją logistyczną~\cite{hastie}: wynik zależy od umiejętności działu, odchyleń poszczególnych umiejętności i trudności zadania "
      r"(długość jego wektora). Kara ridge ściąga oceny do zera przy małej liczbie prób, a starsze próby ważą mniej (połowa wagi po 30 dniach). "
      r"Niepewność daje przybliżenie Laplace'a~\cite{mackay}: dział dostaje ocenę \emph{mocny} lub \emph{słaby} tylko wtedy, gdy cały "
      r"80\% przedział leży po jednej stronie progu; inaczej jest \emph{za mało danych}.")
    A(r"\paragraph{Sfera kompetencji.} Wektory zadań rzutujemy metodą PCA~\cite{jolliffe} do trzech wymiarów i na sferę; wyniki wygładzamy "
      r"harmonikami sferycznymi z karą za krzywiznę~\cite{wahba}. Doliny to luki, szczyty to opanowany materiał.")
    A(r"\paragraph{Sesje i pokrycie.} Sesje wyznaczamy z przerw w czasie zapisu. Pokrycie to odsetek zadań działu w bazie, które uczeń już "
      r"próbował; dział przypisujemy zadaniu tak samo jak w planie nauki.")
    A(r"\paragraph{Powtórki.} Zadania z błędem wracają po 3, 7 i 21 dniach, zgodnie z efektem rozłożenia w czasie~\cite{cepeda}.")

    # ---- 4. wyniki
    A(r"\section{Wyniki}\label{sec:wyniki}")
    A(r"\subsection{Przebieg prób}")
    A(r"Rys.~\ref{fig:os} pokazuje każdą próbę osobno, w kolejności zapisu; kolor oznacza ocenę, a pionowe linie rozdzielają sesje. "
      r"Przy tak krótkiej historii średnia narastająca jest jeszcze zdominowana przez pierwsze próby.")
    A(_rys(wyk2["os_prob"], r"Wynik każdej próby, średnia narastająca i granice sesji (data i godzina początku sesji nad wykresem).", "fig:os"))
    sr_ses = [(s_, [x["sc"] * 10 for x in proby if x["sesja"] == s_]) for s_ in range(ns)]
    if ns >= 2:
        A(f"Średnie sesji: " + ", ".join(f"{sum(v)/len(v):.0f}\\% (n={len(v)})" for _, v in sr_ses)
          + ". Porównanie sesji jest ostrożne: różnią się dobranymi działami i trudnością zadań.")
    pan = [(wyk2["sesje"], "Wyniki w sesjach: romb to średnia, kółka to próby.", "fig:ses-a")]
    pan.append((wyk2["punkty_narastajaco"], "Punkty zdobyte i możliwe narastająco.", "fig:ses-b") if "punkty_narastajaco" in wyk2 else
               (wyk["trend"], "Trend wyniku w czasie.", "fig:ses-b"))
    A(_rys2(pan[0], pan[1], "Sesje i punkty.", "fig:ses"))
    if "trend" in wyk and "punkty_narastajaco" in wyk2:
        A(r"Rys.~\ref{fig:trend} pokazuje te same dane jako średnią kroczącą z 7 dni; przy kilku dniach historii pasmo niepewności jest szerokie.")
        A(_rys(wyk["trend"], r"Wynik zadania w czasie: dzienna średnia, średnia krocząca z 7 dni i progi faz 60/75/90\%.", "fig:trend"))

    A(r"\subsection{Profil umiejętności}")
    if ok_prof:
        b = prof.get("trudnosc_b")
        A(r"Rys.~\ref{fig:profil} zestawia opanowanie działów z modelu i zależność wyniku od trudności. "
          + _zdanie_trudnosci(prof)
          + "Większość przedziałów jest szeroka: model nie ma jeszcze z czego wnioskować.")
        if "trudnosc" in wyk:
            A(_rys2((wyk["profil"], r"Opanowanie działów; jasny pasek to 80\% przedział, w nawiasie liczba prób.", "fig:profil-a"),
                      (wyk["trudnosc"], r"Wynik a trudność; krzywa to przewidywanie modelu z 80\% pasmem niepewności.", "fig:profil-b"),
                      "Profil umiejętności ucznia.", "fig:profil"))
        else:
            A(_rys(wyk["profil"], r"Opanowanie działów z 80\% przedziałem.", "fig:profil", szer=r".6\linewidth"))
        nietkniete = sorted({d["makro_dzial"] for d in D["engine"].dims_list if d.get("segment") == "A"} - {x["dzial"] for x in prof["dzialy"]})
        if nietkniete:
            A(r"Działy bez żadnej próby: " + _tex_raport_calosc(", ".join(nietkniete)) + ".")
        A(r"\begin{table}[!htbp]\centering\caption{Działy: próby, średni wynik i opanowanie z modelu.}\label{tab:dzialy}\small")
        A(r"\begin{tabular}{@{}lrrrrl@{}}\toprule Dział & Próby & Średnia (\%) & Opanowanie (\%) & Przedział & Ocena\\\midrule")
        for x in sorted(prof["dzialy"], key=lambda x: x["opanowanie"]):
            sr = [p_["sc"] * 10 for p_ in proby if p_["dzial"] == x["dzial"]]
            A(f"{_tex_raport_calosc(x['dzial'])} & {x['proby']} & {(f'{sum(sr)/len(sr):.0f}' if sr else '---')} & {x['opanowanie']} & {x['przedzial'][0]}--{x['przedzial'][1]} & {_tag_oceny(x['ocena'])}\\\\")
        A(r"\bottomrule\end{tabular}\end{table}")
        A(r"Kreska w kolumnie średniej oznacza dział, któremu model przypisał próbę ze względu na wektor zadania, choć plan nauki "
          r"zalicza to zadanie do innego działu; średnia i opanowanie liczą się więc różnymi regułami przypisania.")
        um = [x for x in prof.get("umiejetnosci", []) if x["proby"] >= 2]
        if um:
            A(r"\begin{table}[!htbp]\centering\caption{Najsłabsze i najmocniejsze umiejętności (co najmniej 2 próby).}\label{tab:um}\footnotesize")
            A(r"\begin{tabularx}{\linewidth}{@{}Xrr@{\hspace{14pt}}Xrr@{}}\toprule Najsłabsze & \% & Próby & Najmocniejsze & \% & Próby\\\midrule")
            sl, mo = um[:6], um[::-1][:6]
            for i in range(max(len(sl), len(mo))):
                a_ = f"{_tex_raport_calosc(sl[i]['umiejetnosc'][:60])} & {sl[i]['opanowanie']} & {sl[i]['proby']}" if i < len(sl) else "& &"
                b_ = f"{_tex_raport_calosc(mo[i]['umiejetnosc'][:60])} & {mo[i]['opanowanie']} & {mo[i]['proby']}" if i < len(mo) else "& &"
                A(a_ + " & " + b_ + r"\\")
            A(r"\bottomrule\end{tabularx}\end{table}")
        else:
            A(r"\brak{Umiejętności z co najmniej 2 próbami jeszcze nie ma, więc tabeli umiejętności nie budujemy.}")
    else:
        A(r"\brak{Profil działów powstaje z historii prób; pojawi się po kilku sprawdzonych arkuszach.}")

    A(r"\subsection{Dział po dziale, sesja po sesji}")
    A(r"Rys.~\ref{fig:dz-ses} pokazuje, co w której sesji ćwiczono i z jakim skutkiem. Dział pojawiający się w jednej sesji ma jedną "
      r"kropkę: to dopiero pomiar, nie trend.")
    pr_ = [(wyk2["dzial_sesja"], "Dział w sesji: wielkość kropki to liczba prób, kolor i liczba to średni wynik.", "fig:dz-ses-a")]
    pr_.append((wyk["tydzien_dzialy"], "Wszystkie próby w działach: kółko to próba, romb to średnia.", "fig:dz-ses-b")
               if "tydzien_dzialy" in wyk else (wyk2["sesje"], "Sesje.", "fig:dz-ses-b"))
    A(_rys2(pr_[0], pr_[1], "Działy w sesjach i w całości.", "fig:dz-ses"))

    A(r"\subsection{Pokrycie bazy}")
    A(f"Baza liczy {D['baza_n']} zadań; do tej pory zrobiono {k['zadan']} ({100 * k['zadan'] / D['baza_n']:.1f}\\%). "
      r"Rys.~\ref{fig:pokr} pokazuje, które działy i które lata arkuszy są dotknięte. Pokrycie ma znaczenie, bo model nie może "
      r"wnioskować o dziale, którego nie widział.")
    if "pokrycie_lata" in wyk2:
        A(_rys2((wyk2["pokrycie_dzialy"], "Odsetek zadań działu, które już zrobiono (liczby: zrobione z dostępnych).", "fig:pokr-a"),
                  (wyk2["pokrycie_lata"], "Zadania w bazie i zrobione według roku arkusza CKE.", "fig:pokr-b"), "Pokrycie bazy.", "fig:pokr"))
    else:
        A(_rys(wyk2["pokrycie_dzialy"], "Pokrycie bazy po działach.", "fig:pokr", szer=r".55\linewidth"))

    A(r"\subsection{Analiza błędów}")
    bl = [x for x in proby if x["wynik"] != "ok"]
    if "bledy" in wyk and "dzial_typ_bledu" in wyk:
        A(r"Rys.~\ref{fig:bledy} pokazuje typy błędów i działy, w których wystąpiły; Rys.~\ref{fig:bl-ses} dodaje czas.")
        A(_rys2((wyk["bledy"], "Typy błędów w całej historii.", "fig:bledy-a"),
                  (wyk["dzial_typ_bledu"], r"Gdzie występują błędy: wiersz to dział, kolumna to typ, kropka to liczba błędów.", "fig:bledy-b"),
                  "Struktura błędów.", "fig:bledy"))
    elif "bledy" in wyk:
        A(_rys(wyk["bledy"], "Typy błędów w całej historii.", "fig:bledy", szer=r".55\linewidth"))
    if "blad_sesja" in wyk2:
        A(_rys(wyk2["blad_sesja"], "Typy błędów w kolejnych sesjach.", "fig:bl-ses", szer=r".5\linewidth"))
    if bl:
        A(r"\begin{longtable}{@{}rlrrrll@{}}\caption{Wszystkie próby z błędem.}\label{tab:bledy}\\\toprule "
          r"Dzień & Zadanie & id & Dział & Pkt & Typ & Opis\\\midrule\endfirsthead")
        A(r"\toprule Dzień & Zadanie & id & Dział & Pkt & Typ & Opis\\\midrule\endhead\bottomrule\endfoot")
        for x in bl:
            d = _dt_raport_calosc(x["ts"])
            pk_ = f"{x['pkt']:g}/{x['maks']}" if x["maks"] else x["wynik"]
            A(f"{DNI_PL[d.weekday()]} {d:%d.%m} & {_tex_raport_calosc(x['nazwa'][-14:])} & {x['id']} & {_tex_raport_calosc(_skrot_raport_calosc(x['dzial'], 12))} & {pk_} & "
              f"{_tex_raport_calosc(x['typ'] or '—')} & {{\\scriptsize {_tex_raport_calosc(x['opis'][:70])}}}\\\\")
        A(r"\end{longtable}")
    if D["powt"]:
        A(r"Kolejkę zaległych powtórek~\cite{cepeda} podaje Rys.~\ref{fig:powt}.")
        if "powtorki" in wyk:
            A(_rys(wyk["powtorki"], "Zaległe powtórki: dni po terminie (czerwone: ponad 7 dni).", "fig:powt", szer=r".45\linewidth"))
    else:
        A(r"Zaległych powtórek: 0 (żadne zadanie z błędem nie przekroczyło jeszcze terminu 3 dni).")

    A(r"\subsection{Reprezentacje trójwymiarowe}")
    A(_rys(wyk2["proby3d"], "Każda próba jako lizak: oś x to kolejność, oś y dział, wysokość to wynik; kolor to ocena (zielony: komplet).", "fig:p3d"))
    if "krajobraz3d" in wyk:
        A(r"Rys.~\ref{fig:krajobraz} rozkłada model na płaszczyznę dział i trudność; spadek wzdłuż osi trudności wskazuje poziom, od którego dział przestaje być opanowany.")
        A(_rys(wyk["krajobraz3d"], r"Krajobraz szans: przewidywana szansa na komplet punktów w funkcji działu i trudności zadania; "
                 r"przerywane linie to przekroje dla trudności 3, 4 i 5.", "fig:krajobraz"))
    if "ewolucja3d" in wyk:
        A(_rys(wyk["ewolucja3d"], r"Ewolucja opanowania: model dopasowany do historii do końca każdego tygodnia.", "fig:ewol"))
    if "sfera3d" in wyk:
        sf, st = D["sfera"], D["sfera"]["stats"]
        A(r"Sfera kompetencji (Rys.~\ref{fig:sfera}) ma stopień harmonik " + f"{st['stopien_L']}, błąd walidacji krzyżowej ±{st['margines_bledu']} pkt na 10, "
          + f"R2 = {st['r2']} i pokrycie sfery {_tex_raport_calosc(st['pokrycie_sfery'])} (wiarygodność: {_tex_raport_calosc(st['wiarygodnosc'])}). "
          + "Przy tak małej liczbie zadań powierzchnia jest orientacyjna.")
        A(_rys(wyk["sfera3d"], r"Sfera kompetencji: rzut PCA wektorów zadań na sferę z wygładzeniem harmonikami sferycznymi, dwa widoki.", "fig:sfera"))
        if sf["kryt"]:
            A(r"\begin{table}[!htbp]\centering\caption{Luki i mocne strony na sferze kompetencji.}\label{tab:kryt}\small")
            A(r"\begin{tabularx}{\linewidth}{@{}llXr@{}}\toprule & Wynik & Czego dotyczy (główne umiejętności) & Zadanie\\\midrule")
            for cp in sf["kryt"]:
                cz = re.split(r"\s*\((\d+(?:\.\d+)?)%\),?\s*", cp["opis"])
                nazwy = [n.strip() for n in cz[0::2] if n.strip()][:2]
                nazwy = [n if len(n) <= 70 else n[:68].rsplit(" ", 1)[0] + "…" for n in nazwy]
                typ = r"\textcolor{zle}{luka}" if cp["typ"] == "MINIMUM" else r"\textcolor{dobret}{mocna}"
                A(f"{typ} & {cp['wysokosc']:.1f}/10 & {_tex_raport_calosc('; '.join(nazwy))} & {cp.get('rekomendowane_zadanie_id') or '—'}" + r"\\")
            A(r"\bottomrule\end{tabularx}\end{table}")
    if "czas_trudnosc3d" in wyk:
        A(_rys2((wyk["przestrzen3d"], "Przestrzeń zadań bazy (PCA); kolorowe: zadania z historii.", "fig:pca-a"),
                  (wyk["czas_trudnosc3d"], "Czas, trudność i wynik z płaszczyzną trendu (metoda najmniejszych kwadratów).", "fig:pca-b"),
                  "Zadania w przestrzeni pojęć i w czasie.", "fig:pca"))
    else:
        A(_rys(wyk["przestrzen3d"], "Przestrzeń zadań bazy (PCA); kolorowe: zadania z historii.", "fig:pca", szer=r".55\linewidth"))

    # ---- 5. dyskusja
    A(r"\FloatBarrier\section{Dyskusja}\label{sec:dyskusja}")
    if komentarz:
        A("\n\n".join(_tex_z_math(a.strip()) for a in komentarz.strip().split("\n\n") if a.strip()))
    A(r"\paragraph{Rekomendacje.}")
    A(r"\begin{enumerate}")
    k2 = dict(k, proby_tyg=k["lacznie"])
    D2 = dict(D, kpi=k2, tyg=proby)
    for r_ in rekomendacje(D2):
        A(r"\item " + _tex_raport_calosc(r_))
    A(r"\end{enumerate}")
    A(r"\paragraph{Ograniczenia.} Wynik ułamkowy traktujemy jak prawdopodobieństwo; tekst zadań w bazie pochodzi z OCR, więc wektory części zadań są "
      r"zaszumione; przy małej liczbie prób przedziały są szerokie i dział trafia do kategorii \emph{za mało danych}. Ocena punktowa "
      r"pochodzi z jednego sprawdzającego, a próby jednej sesji nie są niezależne (ten sam dzień, ta sama forma).")

    # ---- 6. braki w danych
    A(r"\section{Czego jeszcze nie da się policzyć}\label{sec:braki}")
    brak = []
    brak.append(f"\\textbf{{Pełne arkusze CKE:}} {len(D['pelne'])}. Bez nich nie ma ani wyniku porównywalnego z matury, ani oceny wytrzymałości na 180 minut.")
    brak.append("\\textbf{Czas rozwiązywania:} baza zapisuje tylko chwilę sprawdzenia, więc nie da się policzyć tempa ani zależności wyniku od czasu.")
    n_pow = sum(x["powt"] for x in proby)
    brak.append(f"\\textbf{{Powtórki:}} {n_pow} prób to powtórki. Bez nich nie ma krzywej zapominania ani pomiaru, czy poprawione błędy utrzymują się po 3, 7 i 21 dniach.")
    dz_1 = [d for d in {x['dzial'] for x in proby} if sum(x['dzial'] == d for x in proby) < 3]
    brak.append(f"\\textbf{{Działy z mniej niż 3 próbami:}} {len(dz_1)} z {len({x['dzial'] for x in proby})} ruszonych. Dla nich opanowanie z modelu jest tylko hipotezą.")
    brak.append(f"\\textbf{{Kalibracja modelu:}} porównanie przewidywań z wynikami wymaga co najmniej ok. 30 prób; jest {k['lacznie']}.")
    brak.append(f"\\textbf{{Trend między tygodniami:}} historia obejmuje {(do - od).days + 1} dni, więc zmian tydzień do tygodnia jeszcze nie ma.")
    A(r"\begin{itemize}")
    for b_ in brak:
        A(r"\item " + b_)
    A(r"\end{itemize}")
    A(r"Raport ma ten sam układ niezależnie od liczby prób: wykresy zapełniają się same, a rzeczy, na które brakuje danych, są wymienione powyżej.")

    # ---- 7. dalsza praca
    A(r"\section{Dalsza praca}")
    A(r"Rys.~\ref{fig:harm} przedstawia fazy przygotowań i progi pełnych arkuszy (Tab.~\ref{tab:progi}).")
    A(_rys(wyk["harmonogram"], "Harmonogram 29 tygodni z fazami, progami i pełnymi arkuszami.", "fig:harm"))
    A(r"\begin{table}[!htbp]\centering\caption{Progi faz.}\label{tab:progi}\small")
    A(r"\begin{tabularx}{\linewidth}{@{}lXr@{}}\toprule Do dnia & Próg & Pozostało\\\midrule")
    for d, v in sorted(D["harm"]["cele"].items()):
        dd = date.fromisoformat(d)
        dn = (dd - D["dzis"]).days
        A(f"{dd:%d.%m.%Y} & {_tex_raport_calosc(v)} & {str(dn) + ' dni' if dn >= 0 else 'minął'}\\\\")
    A(r"\bottomrule\end{tabularx}\end{table}")

    A(r"\FloatBarrier")
    A(BIBLIOGRAFIA)
    A(r"\paragraph{Dostępność danych i kodu.} Dane: \texttt{matura.db}, \texttt{dane/harmonogram.json}, \texttt{notatki/postep.md}, \texttt{arkusze/}. "
      r"Raport wygenerowano " + f"{_dt_raport_calosc(time.time()):%d.%m.%Y %H:%M}"
      + r" skryptem \texttt{raport\_calosc.py} (tylko odczyt bazy); wynik zadania: punkty z notatki, a bez nich ok = 100\%, partial = 50\%, fail = 10\%.")
    A(r"\end{document}")
    return "\n".join(L)


def _main_raport_calosc(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dzien", help="data raportu RRRR-MM-DD (domyślnie dziś)")
    ap.add_argument("--symulacja", action="store_true")
    ap.add_argument("--komentarz")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    zapewnij_zaleznosci()
    dzien = date.fromisoformat(a.dzien) if a.dzien else datetime.now(TZ).date()
    teraz = time.time() if not a.dzien else datetime(dzien.year, dzien.month, dzien.day, 9, 47, tzinfo=TZ).timestamp()
    if a.symulacja:
        if not a.dzien:
            dzien = date(2026, 11, 21)
            teraz = datetime(2026, 11, 21, 9, 47, tzinfo=TZ).timestamp()
        kat_bazy = przygotuj_symulacje(dzien)
    else:
        kat_bazy = BASE
        sys.path.insert(0, str(BASE))
    import matura as m
    p = m
    assert Path(m.__file__).resolve().parent == kat_bazy.resolve(), "zły katalog modułów"
    out = Path(a.out) if a.out else RAPORTY / (dzien.isoformat() + ("_calosc_sym" if a.symulacja else "_calosc"))
    out.mkdir(parents=True, exist_ok=True)
    D = zbierz_calosc(m, p, teraz)
    Dw = dict(D, tyg=D["proby"], pop=[], od=_dt_raport_calosc(D["proby"][0]["ts"]).date() if D["proby"] else D["od"])
    wyk = wykresy(Dw, out / "wykresy")
    wyk2 = wykresy_calosc(D, out / "wykresy")
    if not wyk2:
        raise SystemExit("[raport_calosc] baza nie ma żadnej próby: brak czego raportować")
    kom = Path(a.komentarz).read_text(encoding="utf-8") if a.komentarz else None
    tex = zloz_calosc(D, wyk, wyk2, kom, a.symulacja)
    (out / "raport.tex").write_text(tex, encoding="utf-8")
    for _ in range(2):
        r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "raport.tex"], cwd=out,
                           capture_output=True, text=True, errors="replace")
        if r.returncode:
            print(r.stdout[-3000:])
            raise SystemExit("[raport_calosc] BŁĄD: pdflatex się nie powiódł (log: raport.log)")
    for f in ("raport.aux", "raport.out"):
        (out / f).unlink(missing_ok=True)
    pod = out / "podglad"
    shutil.rmtree(pod, ignore_errors=True)
    pod.mkdir()
    if shutil.which("pdftoppm"):
        subprocess.run(["pdftoppm", "-png", "-r", "70", str(out / "raport.pdf"), str(pod / "strona")], check=False)
    kk = D["kpi"]
    dane = {"dzien": dzien.isoformat(), "symulacja": a.symulacja, "kpi": kk, "sesje": D["n_sesji"],
            "pokrycie": {"zadania_w_bazie": D["baza_n"], "zrobione": kk["zadan"]},
            "profil": {x["dzial"]: {kk_: x[kk_] for kk_ in ("opanowanie", "przedzial", "proby", "ocena")} for x in D["profil"].get("dzialy", [])},
            "wykresy": sorted(set(wyk) | set(wyk2)), "strony": len(list(pod.glob("*.png")))}
    (out / "dane.json").write_text(json.dumps(dane, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if a.symulacja:
        shutil.rmtree(kat_bazy, ignore_errors=True)
    print(f"[raport_calosc] OK: {out / 'raport.pdf'} ({dane['strony']} str., {kk['lacznie']} prób)")
    return out


# ==============================================================================
# SEKCJA: kontrola (dawniej matura.py kontrola)
# ==============================================================================

#!/usr/bin/env python3
# Szybka kontrola systemu przed arkuszem lub sprawdzaniem (ok. 5 s). Kod 0 = wszystko działa.
#
#     python3 matura.py kontrola
#
# Sprawdza: biblioteki (instaluje scipy, gdy go brak), spójność matura.db, harmonogram na
# dziś, rezerwę pełnych arkuszy (suma 50 pkt), obrazy zadań, propozycję arkusza na dziś
# i pliki procedury. Niczego nie zapisuje w bazie.


bledy: list[str] = []
uwagi: list[str] = []


def ok(warunek: bool, opis: str, krytyczny: bool = True) -> None:
    if not warunek:
        (bledy if krytyczny else uwagi).append(opis)


def _main_kontrola() -> int:
    for mod in ("numpy", "scipy", "sympy", "reportlab", "PIL"):
        try:
            importlib.import_module(mod)
        except ImportError:
            pak = {"PIL": "pillow"}.get(mod, mod)
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", pak], check=False)
            try:
                importlib.import_module(mod)
                uwagi.append(f"doinstalowano {pak}")
            except ImportError:
                bledy.append(f"brak biblioteki {pak} (pip nie zadziałał)")
    ok(subprocess.run(["which", "pdftoppm"], capture_output=True).returncode == 0, "brak pdftoppm (poppler-utils)")
    if bledy:
        return _podsumuj_kontrole()

    con = sqlite3.connect(BASE / "matura.db", timeout=30)
    ok(con.execute("PRAGMA quick_check").fetchone()[0] == "ok", "matura.db: quick_check nie przechodzi")
    tabele = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("main", "desc", "photos", "done", "history", "cooldown_fails", "dims"):
        ok(t in tabele, f"matura.db: brak tabeli {t}")
    ok("podobne" in tabele and "wektory_auto" in tabele, "brak tabel podobne/wektory_auto: odtwórz matura.db z własnej kopii zapasowej", krytyczny=False)
    ok("latwosc" in tabele, "brak tabeli latwosc: odtwórz matura.db z własnej kopii zapasowej (bez niej system działa jak przed 23.10.2026)",
       krytyczny=False)
    con.close()

    sys.path.insert(0, str(BASE))
    import matura as m
    p = m

    try:  # łatwość krajowa jest dodatkiem: None = zachowanie jak dawniej, więc tylko uwaga
        l = m.Latwosc(217)
        ok(bool(l) and l["zrodlo"] == "CKE", "m.Latwosc(217) nie zwraca danych CKE (matura maj 2025, zad. 1)", krytyczny=False)
    except Exception as e:  # noqa: BLE001
        uwagi.append(f"m.Latwosc: {type(e).__name__}: {e}")
    try:  # wagi CKE też są dodatkiem: bez nich działy mają wagę 1, a prognozy nie ma
        w = m.WagaMatury()
        ok(abs(sum(w.values()) - 50) < 1, f"m.WagaMatury: suma {sum(w.values()):.1f} pkt, nie 50", krytyczny=False)
        ok(m.Prognoza()["status"] in ("ok", "brak_danych"), "m.Prognoza nie zwraca statusu", krytyczny=False)
    except Exception as e:  # noqa: BLE001
        uwagi.append(f"wagi CKE: {type(e).__name__}: {e}")
    t = p.Tydzien()
    ok(not t.get("poza_harmonogramem"), f"dziś ({m.Dzis()}) poza harmonogramem", krytyczny=False)
    for z in p.Harmonogram()["rezerwa_pelnych_arkuszy"]:
        s = sum(x["pkt"] or 0 for x in p.Zestaw(z))
        ok(s == 50, f"zestaw {z}: suma punktów {s}, nie 50")
    braki = [i for i, u in con_urls() if not (BASE / u).exists()]
    ok(not braki, f"brak {len(braki)} obrazów, np. {braki[:3]}")
    try:
        prop = p.Propozycja()
        if prop["typ"] == "codzienny":
            ok(len(prop["sloty"]) >= min(p.Harmonogram()["lekcja"]["zadania"]),
               f"propozycja na dziś ma tylko {len(prop['sloty'])} zadań", krytyczny=False)
    except Exception as e:  # noqa: BLE001
        bledy.append(f"Propozycja() rzuca {type(e).__name__}: {e}")
    for f in ("PROCEDURY.md", "README.md", "notatki/dziennik_bledow.md", "notatki/postep.md"):
        ok((BASE / f).exists(), f"brak pliku {f}")
    _kontrola_mapy()
    try:  # rozgrzewka nie zatrzymuje arkusza, więc tylko uwaga
        ok(len(m.Fiszki()) > 0, "notatki/karty/reguly.md bez fiszek: arkusz powstanie bez rozgrzewki", krytyczny=False)
    except Exception as e:  # noqa: BLE001
        uwagi.append(f"matura.py petla / notatki/karty/reguly.md: {type(e).__name__}: {e}; arkusz powstanie bez rozgrzewki")
    ns = p.Niesprawdzone()
    stare = [k for k in ns if (m.Dzis() - __import__("datetime").date.fromisoformat(k[:10])).days > 7]
    ok(not stare, f"arkusze niesprawdzone ponad 7 dni: {', '.join(stare)}", krytyczny=False)
    return _podsumuj_kontrole()


MAPA = {   # co wolno trzymać w folderze systemu (README.md, „Mapa folderu”); wszystko inne to obcy plik
    "": {"matura.py", "matura.db", "README.md", "PROCEDURY.md", "dane", "notatki", "skille", "baza_obrazy", "arkusze",
         "raporty", ".git", ".gitignore", "__pycache__", "requirements.txt"},
    "dane": {"harmonogram.json", "wadliwe_obrazy.json", "odrzucone.json", "instaluj_tex.sh", "baza_wzorcowa.db"},
    "notatki": {"dziennik_bledow.md", "postep.md", "karty"},
    "notatki/karty": {"reguly.md", "pytania.md", "kombinatoryka_cyfry.md"},
    "skille": {"matura-master"},
}
WZORY = {"arkusze": r"\d{4}-\d\d-\d\d_\d{4}(_przyklad)?", "raporty": r"\d{4}-\d\d-\d\d_(tydzien|calosc|atlas|metoda)(_sym)?"}


def _kontrola_mapy() -> None:
    """Obcy plik = taki, którego nie ma w mapie (README). Zgłaszamy jako uwagę z nazwą i adresem."""
    for rel, dozw in MAPA.items():
        kat = BASE / rel if rel else BASE
        if kat.is_dir():
            obce = sorted(x.name for x in kat.iterdir() if x.name not in dozw)
            ok(not obce, f"obce pliki w {rel or 'folderze systemu'}: {', '.join(obce)} (zobacz README, Mapa folderu)", krytyczny=False)
    for rel, wzor in WZORY.items():
        kat = BASE / rel
        if kat.is_dir():
            zle = sorted(x.name for x in kat.iterdir() if not re.fullmatch(wzor, x.name))
            ok(not zle, f"nazwy spoza wzoru w {rel}/: {', '.join(zle)}", krytyczny=False)
    pyt = sorted(str(x.relative_to(BASE)) for x in BASE.rglob("*.py") if "baza_obrazy" not in x.parts and x.name != "matura.py"
                 and "__pycache__" not in x.parts)
    ok(not pyt, f"dodatkowe skrypty .py (kod ma być tylko w matura.py): {', '.join(pyt)}", krytyczny=False)


def con_urls():
    con = sqlite3.connect(BASE / "matura.db", timeout=30)
    try:
        return con.execute("SELECT id, url FROM photos").fetchall()
    finally:
        con.close()


def _podsumuj_kontrole() -> int:
    for u in uwagi:
        print("UWAGA:", u)
    for b in bledy:
        print("BŁĄD:", b)
    print("OK" if not bledy else f"{len(bledy)} błędów")
    return 1 if bledy else 0
    return 1 if bledy else 0


# ==============================================================================
# SEKCJA: sprzatanie (dawniej sprzatanie.py)
# ==============================================================================

#!/usr/bin/env python3
# Sprzątanie starych arkuszy i raportów.
#
# Domyślnie tylko POKAZUJE listę (próba na sucho). Usuwa dopiero z --usun.
#   python3 matura.py sprzataj [--dni 14] [--usun]
#
# Co zostaje zawsze: matura.db, wyniki w bazie, ANALIZA.md, WYNIK.md, zadania.json, plan_arkusza.json,
# dane.json, komentarz.md oraz raport.pdf (to mała historia i źródło dla matura.py raport).
# Co znika po --dni dniach (liczone z daty w nazwie folderu):
#   arkusze/RRRR-MM-DD_GGMM/: arkusz.pdf i podglad/
#   raporty/RRRR-MM-DD_tydzien/ i _calosc/: wykresy/, podglad/, *.tex, *.log, *.aux
#   raporty/ symulacje i atlasy (*_sym, *_atlas, *_atlas_sym, *_calosc_sym): cały folder
# Nie rusza arkuszy bez WYSLANO.txt (nie wysłane uczniowi), dlatego po wysłaniu arkusza zapisz tam znacznik.

DATA = re.compile(r"(\d{4}-\d{2}-\d{2})")


def stare(nazwa, prog):
    m = DATA.search(nazwa)
    return bool(m) and datetime.strptime(m.group(1), "%Y-%m-%d").date() < prog


def _main_sprzatanie():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dni", type=int, default=14)
    ap.add_argument("--usun", action="store_true")
    a = ap.parse_args()
    prog = date.today() - timedelta(days=a.dni)
    cele = []
    for k in sorted((BASE / "arkusze").glob("*_*")):
        if k.is_dir() and stare(k.name, prog):
            if not (k / "WYSLANO.txt").exists():
                print(f"POMIJAM (brak WYSLANO.txt): {k.name}")
                continue
            cele += [p for p in (k / "arkusz.pdf", k / "podglad") if p.exists()]
    for k in sorted((BASE / "raporty").iterdir()):
        if not k.is_dir() or not stare(k.name, prog):
            continue
        if re.search(r"_(sym|atlas|atlas_sym|calosc_sym)$", k.name):
            cele.append(k)
        else:
            cele += [p for p in list(k.glob("*.tex")) + list(k.glob("*.log")) + list(k.glob("*.aux"))
                     + [k / "wykresy", k / "podglad"] if p.exists()]
    if not cele:
        print(f"Nic starszego niż {a.dni} dni ({prog}).")
        return
    print(("USUWAM" if a.usun else "DO USUNIĘCIA (dodaj --usun)") + f", starsze niż {prog}:")
    for p in cele:
        print(" ", p.relative_to(BASE))
        if a.usun:
            shutil.rmtree(p) if p.is_dir() else p.unlink()


# ==============================================================================
# ZGODNOŚĆ WSTECZNA: stare angielskie nazwy z instrukcji projektu (usunąć po zmianie instrukcji)
# ==============================================================================
Find, Get, Done, History, Dims, Similar, Vec, Stats, Weak = (
    Znajdz, Pobierz, Odnotuj, Ostatnie, Wymiary, ZadaniaPodobne, Wektor, Statystyki, Slabe)
MasteryLandscape = Landscape = AnalyzeMasteryLandscape = CriticalDirections = Opanowanie


# ==============================================================================
# CLI: jedno wejście dla wszystkich narzędzi
# ==============================================================================

# ==============================================================================
# SEKCJA: start (init + plan): przygotowanie czystej kopii repozytorium do pracy
# ==============================================================================

_POSTEP = """# Postęp

Matura: {matura}. Progi faz z `dane/harmonogram.json` (`cele`).

## Pełne arkusze

| Data | Zestaw | Punkty /50 | % | Czas (min) | Najwięcej straconych punktów | Uwagi |
| --- | --- | --- | --- | --- | --- | --- |

## Progi faz

| Do dnia | Próg | Status |
| --- | --- | --- |
{progi}

## Przeglądy

| Data | Próby (14 dni) | Średni wynik | Decyzja |
| --- | --- | --- | --- |

## Zmiany planu

| Data | Zmiana | Powód |
| --- | --- | --- |
"""
_DZIENNIK = """# Dziennik błędów

Każdy błąd ze sprawdzania: jeden wiersz, najnowszy na górze. Kolumna „Reguła” ma formę „Jeśli <sygnał>, to <odruch>.”.
Typy: rachunkowy · pojęciowy · metoda · warunki · niedokończone · odczyt · zapis

## Stałe reguły

## Błędy

| Data | Arkusz | id | Dział | Typ | Co się stało | Reguła |
| --- | --- | --- | --- | --- | --- | --- |
"""
_REGULY = """# Fiszki reguł (rozgrzewka na arkuszu)

Jedna reguła z dziennika błędów = jedna fiszka „Jeśli …, to …”. Stan powtórek prowadzi `python3 matura.py petla`; ręcznie zmieniaj tylko treść.

| Nr | Jeśli … | to … | Przykład | Typ | Dział | Seria | Ostatnio | Następna | Pokazana |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
"""


def _main_init() -> int:
    """init [--od RRRR-MM-DD]: tworzy matura.db z wzorca (bez historii) i puste pliki notatek i foldery robocze; z --od przesuwa harmonogram (patrz _main_plan)."""
    ap = argparse.ArgumentParser(prog="matura.py init", description=_main_init.__doc__)
    ap.add_argument("--od", help="data poniedziałku pierwszego tygodnia planu (RRRR-MM-DD); domyślnie plan bez zmian")
    a = ap.parse_args()
    for d in (NOTATKI / "karty", BASE / "arkusze", BASE / "raporty"):
        d.mkdir(parents=True, exist_ok=True)
    if not DB.exists():
        shutil.copy(DANE / "baza_wzorcowa.db", DB)
        print("utworzono: matura.db (kopia dane/baza_wzorcowa.db, bez historii prób)")
    h = json.loads(HARMONOGRAM.read_text(encoding="utf-8"))
    pliki = {
        NOTATKI / "postep.md": _POSTEP.format(matura=h["matura"].replace("T", ", "), progi="\n".join(
            f"| {d} | {t} | |" for d, t in h["cele"].items())),
        NOTATKI / "dziennik_bledow.md": _DZIENNIK,
        NOTATKI / "karty" / "reguly.md": _REGULY,
        NOTATKI / "karty" / "pytania.md": NAGLOWEK_PYTAN + "\n| " + " | ".join(KOLUMNY_PYTAN) + " |\n| "
        + " | ".join("---" for _ in KOLUMNY_PYTAN) + " |\n",
    }
    for p_, tresc in pliki.items():
        if p_.exists():
            print(f"zostaje: {p_.relative_to(BASE)}")
        else:
            p_.write_text(tresc, encoding="utf-8")
            print(f"utworzono: {p_.relative_to(BASE)}")
    if a.od:
        _przesun_plan(a.od)
    return 0


def _przesun_plan(od: str) -> None:
    """Przesuwa wszystkie daty harmonogramu tak, by tydzień 1 zaczynał się w poniedziałek `od`
    (daty matury nie ruszamy; liczba tygodni zostaje, więc przy innym terminie skróć lub wydłuż plan przez Skroc/Wydluz)."""
    nowy = date.fromisoformat(od)
    if nowy.weekday() != 0:
        raise SystemExit(f"{od} nie jest poniedziałkiem")
    h = json.loads(HARMONOGRAM.read_text(encoding="utf-8"))
    delta = nowy - date.fromisoformat(h["tygodnie"][0]["od"])
    for t in h["tygodnie"]:
        t["od"] = (date.fromisoformat(t["od"]) + delta).isoformat()
        t["do"] = (date.fromisoformat(t["do"]) + delta).isoformat()
    h["cele"] = {(date.fromisoformat(k) + delta).isoformat(): v for k, v in h["cele"].items()}
    HARMONOGRAM.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"harmonogram przesunięty o {delta.days} dni: tydzień 1 od {od}, ostatni do {h['tygodnie'][-1]['do']}; matura {h['matura']}")
    if h["tygodnie"][-1]["do"] > h["matura"][:10]:
        print(f"UWAGA: plan kończy się po terminie matury ({h['matura'][:10]}): skróć go przez m.Skroc albo wybierz wcześniejszy poniedziałek.")
    print("Dostosuj `matura`, `lekcje` i `cele` w dane/harmonogram.json do własnej sytuacji; progi w notatki/postep.md.")


def _main_plan() -> int:
    ap = argparse.ArgumentParser(prog="matura.py plan", description="Przesuwa daty harmonogramu: plan --od PONIEDZIAŁEK")
    ap.add_argument("--od", required=True, help="poniedziałek pierwszego tygodnia (RRRR-MM-DD)")
    _przesun_plan(ap.parse_args().od)
    return 0


m = p = R = petla = sys.modules[__name__]   # dawne moduły (matura, plan, raport, petla) to teraz jeden plik

POLECENIA = {
    "kontrola": "_main_kontrola", "arkusz": "_main_arkusz", "petla": "_main_petla", "raport": "_main_raport",
    "init": "_main_init", "plan": "_main_plan", "raport-calosc": "_main_raport_calosc", "raport-atlas": "_main_raport_atlas", "sprzataj": "_main_sprzatanie",
}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in POLECENIA:
        print("Użycie: python3 matura.py <polecenie> [opcje]\nPolecenia: " + ", ".join(POLECENIA))
        return 2
    sys.argv = [f"matura.py {argv[0]}"] + argv[1:]
    sys.modules.setdefault("matura", sys.modules[__name__])
    r = globals()[POLECENIA[argv[0]]]()
    return r if isinstance(r, int) else 0  # raporty zwracają ścieżkę wyniku: to sukces, nie kod błędu


if __name__ == "__main__":
    sys.exit(main())
