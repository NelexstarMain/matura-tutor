# matura-tutor

Narzędzie do nauki do matury rozszerzonej z matematyki (CKE), które pracuje razem z Claude'em. Claude jest korepetytorem (metoda sokratyczna), a ten folder daje mu pamięć i narzędzia: bazę zadań z arkuszy CKE, dobór zadań pod ucznia, arkusze PDF do wydruku, sprawdzanie zdjęć rozwiązań, dziennik błędów, powtórki i raporty z wykresami. Wgrywasz go do własnego Claude'a (Claude Code, projekt z dostępem do plików albo Claude w chmurze) i uczysz się dalej od swojego poziomu.

## Jak to działa

1. **Baza** (`dane/baza_wzorcowa.db`, `baza_obrazy/`): 4608 zadań z lat 2002–2026, każde jako obraz (źródło treści) i wektor 160 wymiarów (120 działów i tematów, 34 narzędzia, 6 stylów). Pole `latex` to często zepsuty OCR, dlatego Claude czyta obraz.
2. **Model ucznia**: każda próba (`ok`, `partial`, `fail`, punkty, typ błędu) trafia do tabel `done`/`history` w `matura.db`. Z historii liczy się opanowanie działów z przedziałem ufności, trudność zadań wobec przeciętnego maturzysty (łatwość CKE), wagi działów z matur 2023–2026 i prognoza wyniku.
3. **Dobór**: `Propozycja` składa arkusz według harmonogramu 29 tygodni (diagnoza, nowe działy, trudne typy, pełne arkusze, szlif) i luk ucznia; powtórki wracają po 3, 7 i 21 dniach w postaci bliźniaczych zadań.
4. **Arkusz**: `matura.py arkusz` składa PDF z obrazów zadań (przycięte, z metryczką: plan, pewność, kontrola) oraz pięcioma pytaniami rozgrzewki z reguł własnych błędów ucznia.
5. **Sprawdzanie**: uczeń robi zdjęcia rozwiązań, Claude ocenia jak CKE, pyta naprowadzająco, zapisuje wynik, dopisuje błąd do dziennika i fiszkę „Jeśli …, to …”.
6. **Raporty**: PDF z LaTeX-a z wykresami 2D i 3D (tydzień, cała historia, atlas bazy).

Założenia: uczeń zdaje maturę w Polsce (czas polski), pracuje na wydrukowanych arkuszach, rozwiązuje zadania otwarte; zakres wymagań to formuła 2023 z ograniczeniami od 2025 (bez nierówności trygonometrycznych i przybliżania miejsc zerowych); zakres 2027 nie jest potwierdzony. Domyślny harmonogram jest przykładem (matura 11.05.2027, start 19.10.2026): zmień go pod siebie.

## Uruchomienie od zera

Wymagania: Python 3.10+, `poppler-utils` (`pdftoppm`); do raportów dodatkowo TeX Live (`bash dane/instaluj_tex.sh`, ok. 600 MB).

```bash
git clone <adres repo> && cd matura-tutor
pip install -r requirements.txt
python3 matura.py start                    # krok ręczny: robi to samo co hook (init + kontrola + stan ucznia)
python3 matura.py plan --od 2026-11-02     # daty planu od wybranego poniedziałku
```

Najprościej: otwórz folder w Claude Code (albo wgraj repo do projektu Claude). Wszystko ładuje się samo: `CLAUDE.md` mówi Claude'owi, czym jest ten system, hook `SessionStart` (`.claude/settings.json`) odpala `python3 matura.py start` (instalacja zależności, `matura.db` z wzorca, kontrola, stan ucznia), a skill `matura-master` leży w `.claude/skills/`. Nowemu uczniowi Claude zadaje pytania wstępne po jednym i układa plan. Bez hooka (inne klienty) wystarczy powiedzieć Claude'owi: „Uruchom `python3 matura.py start` i zacznij.”

Najczęstsze polecenia: `python3 matura.py arkusz --ids ... --out arkusze/RRRR-MM-DD_GGMM/arkusz.pdf`, `python3 matura.py raport`, `python3 matura.py petla dzis`, `python3 matura.py plan --od PONIEDZIAŁEK`. Pełna lista funkcji i poleceń: `python3 matura.py` bez argumentów oraz kod w `matura.py` (każda funkcja ma docstring po polsku); kroki procedur: `PROCEDURY.md`.

## Co jest w repozytorium

| Ścieżka | Po co |
| --- | --- |
| `matura.py` | cały kod: biblioteka i CLI (`start`, `init`, `plan`, `kontrola`, `arkusz`, `petla`, `raport`, `raport-calosc`, `raport-atlas`, `sprzataj`) |
| `PROCEDURY.md` | kroki: arkusz, sprawdzanie, rozgrzewka, raport, przegląd planu |
| `CLAUDE.md`, `.claude/` | instrukcja ładowana automatycznie, hook startowy i master skill (`.claude/skills/matura-master/`) |
| `dane/` | wzorcowa baza bez historii prób, harmonogram, listy odrzuconych i wadliwych obrazów |
| `baza_obrazy/` | obrazy zadań (nie ruszać, ścieżki są w bazie) |
| `requirements.txt` | zależności Pythona |

Powstają lokalnie i są w `.gitignore`: `matura.db` (twoja baza z historią), `notatki/` (dziennik błędów, fiszki, postęp), `arkusze/`, `raporty/`.

## Prywatność i prawa autorskie

- Twoje postępy żyją tylko w lokalnym `matura.db` i `notatki/`; nic nie jest wysyłane poza rozmowę z Claude'em. Przed wypchnięciem forka sprawdź, czy nie dodałeś ich do gita.
- Obrazy i treści zadań to arkusze maturalne CKE (cke.gov.pl), publikowane przez CKE do nauki. Repozytorium jest do użytku edukacyjnego; nie publikuj go jako własności ani w celach komercyjnych. Właściciel repo odpowiada za zasadność publicznego udostępniania tych materiałów, dlatego zalecane jest repo prywatne.
- Wyniki „na tle kraju” opierają się na sprawozdaniach CKE (matury 2023–2026) i są szacunkiem (błąd ok. 11 pkt proc.).

## Znane ograniczenia

- Część rekordów to fragmenty rozwiązań albo ucięte kadry: `Szukaj` je pomija, a kolejne odrzucasz przez `m.Odrzuc(id, powód)`.
- `matura.py arkusz` czasem ucina znak na końcu wiersza przy prawej krawędzi kadru: sprawdzaj podgląd (`podglad/strona-N.png`).
- Kolumna `year` w bazie jest błędna (wszędzie 2026); rok jest w nazwie zadania.
