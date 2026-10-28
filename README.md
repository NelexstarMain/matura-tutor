<div align="center">

<img src="docs/banner.svg" alt="matura-tutor: matura z matematyki na 100%" width="100%">

<br>

<a href="LICENSE"><img alt="Licencja MIT" src="https://img.shields.io/badge/licencja-MIT-2B3BFF?style=for-the-badge"></a>
<img alt="4608 zadań" src="https://img.shields.io/badge/zadania-4608-FF5C8A?style=for-the-badge">
<img alt="Działa z Claude" src="https://img.shields.io/badge/dzia%C5%82a%20z-Claude-12C8A0?style=for-the-badge">
<img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-FFD23F?style=for-the-badge&labelColor=0E1330">

</div>

<br>

<h2 align="center">Cztery kroki, które się powtarzają</h2>

<img src="docs/kroki.svg" alt="Arkusz, zdjęcia, sprawdzanie, raport" width="100%">

<p align="center"><sub>Potem od nowa, ale z nowym profilem. Im więcej zadań zrobisz, tym trafniej system dobiera następne.</sub></p>

<h2 align="center">Start</h2>

```bash
git clone https://github.com/NelexstarMain/matura-tutor && cd matura-tutor
claude    # reszta dzieje się sama
```

<p align="center">Folder ładuje się automatycznie: <code>CLAUDE.md</code>, hook startowy i skill <code>matura-master</code> są w repozytorium.<br>W innym kliencie powiedz Claude'owi: <i>„Uruchom <code>python3 matura.py start</code> i zacznij.”</i></p>

<h2 align="center">Co w środku</h2>

<table>
<tr>
<td width="33%" valign="top"><h3>🎯 Baza</h3>4608 zadań z arkuszy CKE (2002–2026), każde jako obraz z tagami w 160 wymiarach.</td>
<td width="33%" valign="top"><h3>🧠 Pamięć</h3>Opanowanie działów, powtórki po 3, 7 i 21 dniach, każdy błąd jako reguła „Jeśli …, to …”.</td>
<td width="33%" valign="top"><h3>📅 Plan</h3>29 tygodni od diagnozy do pełnych arkuszy próbnych, daty ustawiasz jednym poleceniem.</td>
</tr>
</table>

| Plik | Po co |
| --- | --- |
| `matura.py` | cały kod i CLI |
| `PROCEDURY.md` | kroki: arkusz, sprawdzanie, rozgrzewka, raport, przegląd planu |
| `docs/DOKUMENTACJA.md` | zasady, mapa folderu, CLI, spis funkcji, pułapki |
| `CLAUDE.md`, `.claude/` | instrukcja, hook startowy, skill |
| `dane/`, `baza_obrazy/` | baza zadań bez historii prób |

Wymagania: Python 3.10+, `poppler-utils`; do raportów TeX Live (`bash dane/instaluj_tex.sh`).

<h2 align="center">Prywatność i licencja</h2>

<p align="center">Twoje postępy (<code>matura.db</code>, <code>notatki/</code>, <code>arkusze/</code>, <code>raporty/</code>) zostają lokalnie i są w <code>.gitignore</code>.<br>Kod, dokumentacja i skill: <a href="LICENSE">MIT</a>. Zadania i obrazy to arkusze CKE, służą do nauki i nie są objęte licencją.</p>
