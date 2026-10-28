<p align="center"><img src="docs/banner.svg" alt="matura-tutor: korepetytor AI do matury rozszerzonej z matematyki" width="100%"></p>

<p align="center">
<a href="LICENSE"><img alt="Licencja MIT" src="https://img.shields.io/badge/licencja-MIT-3B4BFF?style=flat-square&labelColor=0E1330"></a>
<img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3B4BFF?style=flat-square&labelColor=0E1330">
<img alt="Claude Code" src="https://img.shields.io/badge/Claude%20Code-gotowe-3B4BFF?style=flat-square&labelColor=0E1330">
</p>

<p align="center"><img src="docs/liczby.svg" alt="4608 zadań CKE, 160 wymiarów, powtórki po 3, 7 i 21 dniach, 29 tygodni planu" width="100%"></p>

## Start

```bash
git clone https://github.com/NelexstarMain/matura-tutor && cd matura-tutor
claude
```

Reszta dzieje się sama. `CLAUDE.md` mówi Claude'owi, czym jest system, hook startowy uruchamia `python3 matura.py start` (zależności, baza, kontrola, stan ucznia), a skill `matura-master` leży w `.claude/skills/`. W innym kliencie wystarczy napisać: *Uruchom `python3 matura.py start` i zacznij.*

<p align="center"><img src="docs/kroki.svg" alt="Arkusz, rozwiązanie, sprawdzanie, raport, i od nowa" width="100%"></p>

## Zawartość

| | |
| --- | --- |
| `matura.py` | cały kod i CLI |
| `PROCEDURY.md` | arkusz, sprawdzanie, rozgrzewka, raport, przegląd planu |
| `docs/DOKUMENTACJA.md` | zasady, mapa folderu, CLI, funkcje, pułapki |
| `CLAUDE.md`, `.claude/` | instrukcja, hook startowy, skill |
| `dane/`, `baza_obrazy/` | baza zadań bez historii prób |

Wymagania: Python 3.10+ i `poppler-utils`; do raportów PDF także TeX Live (`bash dane/instaluj_tex.sh`).

## Prywatność i licencja

Postępy ucznia (`matura.db`, `notatki/`, `arkusze/`, `raporty/`) zostają na jego komputerze i są w `.gitignore`. Kod, dokumentacja i skill są na licencji [MIT](LICENSE). Zadania i obrazy pochodzą z arkuszy CKE, służą do nauki i nie są objęte licencją.
