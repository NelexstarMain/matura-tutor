# Dokumentacja techniczna: zasady, mapa, CLI, funkcje, pułapki

Polecenia CLI uruchamiaj z głównego folderu repozytorium. Procedury dla sesji: `PROCEDURY.md`.

## Zasady dla agenta

- **Gdzie wolno tworzyć.** Wyniki arkusza tylko w `arkusze/RRRR-MM-DD_GGMM/`, raporty tylko w `raporty/RRRR-MM-DD_rodzaj/`, notatki ucznia tylko w `notatki/`, rzeczy tymczasowe w katalogu roboczym sesji. Nigdy w głównym folderze, w `dane/` ani w `baza_obrazy/`.
- **Co wolno zmieniać.** `matura.db` tylko przez funkcje biblioteki (`Zapisz`, `Odrzuc`), `notatki/` przez polecenia, `arkusze/` i `raporty/` własnych uruchomień. Kod `matura.py` i `PROCEDURY.md` zmienia tylko wątek, o który uczeń poprosił, po kopii zapasowej.
- **Czego nie wolno.** Tworzyć nowych skryptów .py (nowa funkcja trafia do `matura.py`); edytować `baza_obrazy/`; zmieniać harmonogramu poza przeglądem planu albo na prośbę ucznia; dotykać `zadania.json` po złożeniu arkusza; zapisywać kluczy API w plikach.
- **Nazwy.** Małe litery bez polskich znaków w nazwach plików i folderów (wyjątki: `README.md`, `PROCEDURY.md`, pliki wynikowe `ANALIZA.md`, `WYNIK.md`, `WYSLANO.txt`), daty `RRRR-MM-DD`. Plik spoza mapy jest błędem; `python3 matura.py kontrola` go wypisze.

Kontekst: matura rozszerzona (180 min, 50 pkt, zadania otwarte; wymagania ograniczone od 2025: bez nierówności trygonometrycznych i przybliżania miejsc zerowych; zakres 2027 niepotwierdzony). Po polsku, krótko, wzory w LaTeX (`$...$`).

## Mapa folderu

| Ścieżka | Po co |
| --- | --- |
| `matura.py` | cała biblioteka i jedyne wejście CLI (dawne: matura, plan, petla, arkusz, raport, raport_calosc, raport_atlas, kontrola, sprzatanie) |
| `matura.db` | (tworzona lokalnie z `dane/baza_wzorcowa.db`, w `.gitignore`) baza 4608 zadań (wektory 160D, treści, zdjęcia) i historia prób |
| `README.md` | ten plik: zasady, mapa, CLI, spis funkcji, pułapki |
| `PROCEDURY.md` | procedury: `## Arkusz`, `## Sprawdzanie`, `## Rozgrzewka`, `## Raport`, `## Przegląd planu` |
| `dane/harmonogram.json` | 29 tygodni do matury: faza, blok, klucze `Znajdz`, trudność, pełne arkusze, `zestawy_cke` |
| `dane/wadliwe_obrazy.json` | skan uciętych kadrów (`Szukaj` woli kadry spoza listy) |
| `dane/odrzucone.json` | zadania obejrzane i odrzucone (`Odrzuc`); `Szukaj` ich nie podaje |
| `dane/instaluj_tex.sh` | instaluje TeX Live dla raportów (woła go `raport`) |
| `notatki/dziennik_bledow.md` | każdy błąd: typ i reguła „Jeśli …, to …” (sprawdzanie) |
| `notatki/postep.md` | pełne arkusze, progi faz, przeglądy, zmiany planu |
| `notatki/karty/reguly.md` | fiszki „Jeśli …, to …” ze stanem powtórek (`petla`) |
| `notatki/karty/pytania.md` | dziennik każdego pytania rozgrzewki; na nim stoi kontrola powtórek (`petla`) |
| `notatki/karty/kombinatoryka_cyfry.md` | karta metody (format niżej) |
| `.claude/`, `CLAUDE.md` | skill `matura-master`, hook startowy, instrukcja ładowana automatycznie |
| `baza_obrazy/` | 5208 PNG zadań; nie ruszać, ścieżki są w bazie |
| `arkusze/RRRR-MM-DD_GGMM/` | folder arkusza: `zadania.json`, `plan_arkusza.json`, `arkusz.pdf`, `podglad/`, `ANALIZA.md`, `WYNIK.md`, `WYSLANO.txt`; opcjonalnie `kontekst.json`, `rozgrzewka.json`, `pytania.json`, `kontrola/` (kadry dolnej krawędzi) |
| `raporty/RRRR-MM-DD_rodzaj/` | rodzaje `tydzien`, `calosc`, `atlas`, `metoda`; `_sym` = symulacja (np. `RRRR-MM-DD_tydzien_sym`); w środku `raport.pdf`, `raport.tex`, `dane.json`, `komentarz.md`, `wykresy/`, `podglad/` |


Karta metody (jedna na typ zadania, np. `trygonometria_rownania.md`; maks. jedna strona; powstaje przy pierwszym błędzie `pojęciowy` lub `metoda` w dziale, kolejne błędy ją uzupełniają): 1. Kiedy stosować (po czym rozpoznać). 2. Schemat w 3–6 krokach, wzory w LaTeX. 3. Pułapki: co CKE punktuje osobno. 4. Jeden krótki przykład z bazy (id), bez pełnego rozwiązania zadań z arkusza.

## CLI

`python3 matura.py <polecenie> [opcje]`

| Polecenie | Co robi |
| --- | --- |
| `start` | jedno polecenie na początek sesji: zależności, `matura.db` z wzorca, notatki, kontrola, stan ucznia (woła je hook) |
| `init [--od PON]` | tworzy `matura.db` z `dane/baza_wzorcowa.db` i puste notatki |
| `plan --od PON` | przesuwa daty harmonogramu na wskazany poniedziałek |
| `kontrola` | test systemu (ok. 5 s) i kontrola mapy folderu; krok 0 każdej procedury |
| `arkusz --ids ID... --out PDF [--tytul T] [--czas MIN] [--regula R]... [--powt ID:PIERWOTNE...] [--akceptuj ID...] [--rysunek ID...] [--rozgrzewka N] [--bez-bramek] [--bez-pol] [--test]` | składa PDF arkusza; `--rozgrzewka 0` bez rozgrzewki; `--regula` do 3 razy; `--test` nie zapisuje fiszek ani dziennika pytań |
| `petla dzis [-n N]`, `petla lista` | fiszki do powtórki dziś / wszystkie |
| `petla rozgrzewka FOLDER [-n N]` | 5 fiszek na lekcję: formy do wyboru i ostatnie pytania |
| `petla pytania FOLDER` | kontrola `pytania.json` przed składem arkusza |
| `petla ocen FOLDER NR:tak NR:nie ... [--popraw]` | ocena rozgrzewki |
| `petla dodaj "SYGNAŁ" "ODRUCH" [--przyklad T] [--typ TYP] [--dzial D]` | nowa fiszka |
| `petla wroc NR` | błąd z fiszki wrócił |
| `petla czasy NR:GG:MM ... [--koniec GG:MM]` | minuty na zadanie z godzin startu |
| `petla kontekst FOLDER [--start] [--koniec] [--sen] [--zm-start] [--zm-koniec] [--najtrudniejsze] [--pytanie]` | dane lekcji do `kontekst.json` |
| `petla bramki FOLDER [--plan NR:tak\|nie ...] [--kontrola NR:tak\|nie ...]` | sekcja „Bramki i kontekst” w `WYNIK.md` |
| `raport [--symulacja] [--dzien RRRR-MM-DD] [--komentarz PLIK] [--out FOLDER]` | raport tygodniowy PDF (wynik `raporty/RRRR-MM-DD_tydzien/`) |
| `raport-calosc [--symulacja] [--dzien] [--komentarz] [--out]` | raport z całej historii (`_calosc`), tylko na prośbę ucznia |
| `raport-atlas [--symulacja] [--dzien] [--out]` | atlas całej bazy zadań z nałożonymi próbami ucznia (`_atlas`) |
| `sprzataj [--dni N] [--usun]` | lista starych plików (domyślnie > 14 dni, próba na sucho); `--usun` usuwa |

Z Pythona: `import sys; sys.path.insert(0, "<folder repo>"); import matura as m` (jeden moduł: dawne `p.X`, `petla.X` to `m.X`). Brak `scipy` → `python3 -m pip install -q scipy` (albo `kontrola`).

## Spis funkcji publicznych matura.py

**Baza i wyszukiwanie (niska warstwa)**
- `Znajdz(vector, length=None, samples=1, **filtry)`: ID niezrobionych zadań najbliższych profilowi i normie trudności `length` (1,5–6,0+); losowość 0,15; wyklucza zrobione i świeże fail (kwarantanna 3 dni); filtry `exclude`, `level`, `only_dims`, `forbid_dims`, `diversity`.
- `Pobierz(ids, type="text"|"photos"|"both")`: treść LaTeX lub obrazy. `Odnotuj(id, result="ok", note="", score=None)`: niski zapis próby (`ok`/`partial`/`fail`); zwykle `Zapisz`.
- `Ostatnie(n=20, id=None)`: ostatnie próby z notatkami. `Info(id)`: nazwa, tagi, norma, ostatni wynik, punkty, pochodzenie wektora. `Statystyki()`: liczby bazy i prób.
- `Wymiary(segment=None)`: 160 wymiarów (A działy, B narzędzia, C styl). `Wektor(spec)`: słownik kluczy → wektor 160D. `ZadaniaPodobne(id, samples=1, mode="twin"|"harder"|"easier"|"same_tools")`.
- `Punkty(id)`: maks. punkty z nagłówka (`None`, gdy brak). `Zamkniete(id)`: zadanie zamknięte. `Dzis()`: data w Polsce (nie UTC).

**Opanowanie, profil, prognoza**
- `Slabe(limit=5, min_attempts=1, segmenty="AB")`: tematy z największą liczbą błędów.
- `Opanowanie(samples=None, L=None, alpha=0.08, top_tags=4, min_samples=3)`: sferyczny model kompetencji, `["raport_tekstowy"]`.
- `Profil(prog_mocny=0.75, prog_slaby=0.55, proby=None, teraz=None, trudnosc=None)`: opanowanie działów i umiejętności w % z 80% przedziałem (trudność z łatwości kraju, świeżość połowi się co 30 dni), klasy mocne / słabe / niepewne; `trudnosc="norma"` = stary model.
- `Latwosc(id)`: łatwość kraju w % ze źródłem `CKE`/`bliźniak`/`szacunek` (błąd ok. 11 pkt proc.) albo `None`; `LatwoscTekst(id)`.
- `WagaMatury(dzial=None)`: punkty działu na arkuszu 50 pkt (średnia matur 2023–2026; brak działu → 0,5; bez tabeli `latwosc` wszędzie 1,0). `Centyl(procent, rok=None)`. `Prognoza(proby=None, teraz=None, n_los=400)`: % na maturach 2023–2026, przedział 80%, centyl, `tekst`.
- `Pewniaki(min_prob=3)`: 9 typów zadań z każdej matury 2023–2026 i ich liczba w historii. `NaTleKraju(dni=None)`: punkty wobec przeciętnego maturzysty, wynik w zadaniach o łatwości < 30%.

**Plan i dobór**
- `Harmonogram()`, `Tydzien(dzien=None)` (blok, klucze, `length`, `pelny`, faza), `ZestawyCKE()`, `Rezerwa()`, `Podobne(ids, tylko_duplikaty=False)`, `Smieci()`, `Odrzucone()`, `Wykluczone(od_roku=2015)`, `Niesprawdzone()` (folder → id bez wpisu).
- `Szukaj(spec, length=None, samples=4, od_roku=2015, exclude=(), otwarte=True, czyste=True, latwosc=None)`: `Znajdz` z wykluczeniami; `latwosc=(od, do)` najpierw zadania z zakresu. `Blizniak(id, samples=3, tryb="twin"|"harder"|"easier", exclude=())`.
- `Powtorki(dzis=None)`: kolejka 3/7/21 dni, `priorytet` = waga działu × (0,5 + łatwość kraju) × (1 + dni zaległości / 7).
- `Propozycja(dzien=None, godzina=None, folder=None)`: plan arkusza (`typ` codzienny / pelny / pelny_czesc_2, `sloty` z rolą, kluczem, `length`, 4 kandydatami, `decyzje`, `tabela`); z `folder` zapisuje `plan_arkusza.json`.
- `Zestaw(nazwa)`, `Podzial(nazwa)`: pełny arkusz (50 pkt) i podział na dwie lekcje. `Dzial(id)`, `Dzial_klucza(spec)`, `Pokrycie()`, `Punkty_plan(id)`, `Odrzuc(id, powod)`.
- `Popraw(id, pkt, maks, typ, opis, powt, pomoc)`: poprawia ostatni zapis zadania (pomyłka w ocenie) zamiast nowej próby; argumenty jak w `Zapisz`. `Wydluz(tydzien, powod)`, `Skroc(tydzien, powod)`: blok dłużej / wypada (same dopisują „Zmiany planu”). `Przeglad(dni=14)`: dane do przeglądu.

**Analiza i wyniki**
- `Raport(dni=14)`: dane do analizy postępów w jednym tekście (nic nie zapisuje).
- `Zapisz(id, pkt, maks, typ="", opis="", powt=None, pomoc=False)`: zapis z punktów, notatka `[pkt: x/10] [typ] opis; pkt/maks pkt; powt:<id>`; `ok` = komplet bez pomocy, `fail` = 0 pkt, reszta `partial`.
- `Wynik(folder, zapisz=True)`: `WYNIK.md` z prób po utworzeniu arkusza (tabela, suma, %, typy, „Na tle kraju”); nadpisuje cały plik.

**Pętla błędu i rozgrzewka**
- `Fiszki()`, `Dodaj(jesli, to, przyklad="", typ="", dzial="")` (zwraca numer; ValueError, gdy fiszka jest → `Wroc(nr)`), `Wroc(nr)`.
- `Wybierz(n=5, folder=None)`, `DoPytan(n=5, folder=None)` (z formami i ostatnimi pytaniami), `Historia(fiszka=None, bez_arkusza=None)`, `Pytania(folder)`, `Sprawdz(folder)` → (błędy, uwagi), `Pokaz(nry, folder)`, `Ocen(folder, wyniki, popraw=False)`.
- `Czasy(starty, koniec=None)`, `Kontekst(folder, **pola)`, `Bramki(folder, plan=None, kontrola=None, zapisz=True)`.

## Pułapki systemu

- Tekst zadania (`Pobierz(..., 'text')`) to często zepsuty OCR; źródłem treści jest obraz (`'photos'`, oglądaj Read). Część rekordów to fragmenty rozwiązań, wstępy stron („W każdym z zadań od 1. do 4.…”) albo ucięte kadry: `Odrzuc`. Treści usunięte z wymagań (nierówności trygonometryczne, przybliżanie miejsca zerowego): odrzucaj.
- `CKE_RRRR_05`/`_06` miesza kilka arkuszy i podstrony (`_1`, `_2`; `_1` to dalsze strony zadania); numer w nazwie nie zawsze zgadza się z numerem na obrazie. `CKE_RRRR_Z01`…`Z09` (2009–2026) to kompletne zestawy po 50 pkt z punktami w treści („ZADANIE 4 (5 PKT)”); 2023–2026 mają formę matury od 2023. Zestawy 2025–2026, matury `CKE_2025_05`, `CKE_2026_05` oraz `CKE_2026_Z01`, `Z05` są w rezerwie na pełne arkusze (`Rezerwa()`).
- Obraz zadania 12.1 jest zły (rekordy 228/229 i 58 pokazują 12.2): zadanie 12 to cały obraz z arkusza formuły 2015 (id 187 i 46, ta sama treść jako a) i b)); 228–230 i 58–59 wykluczone z codziennych arkuszy. Obrazy matur z rezerwy to całe strony: `arkusz --akceptuj <wszystkie id>`, dla 2026 też `--rysunek 46` (rysunek w dużej pustce nie jest ucinany); `Propozycja` podaje te opcje w `decyzje`.
- `solution` bywa placeholderem („Zwektoryzowano…”), nie kluczem. Kolumna `year` jest błędna (wszędzie 2026); rok jest w nazwie.
- 261 rekordów (m.in. matury 2023–2026) miało zerowy wektor i dostało wektor z 7 podobnych zadań (`wektory_auto`, `Info(id)["wektor"]`; dział trafiony w ok. 77%); 63 to śmieci OCR. Te same zadania są w kilku zestawach: tabela `podobne` (328 duplikatów, 99 wariantów), `Podobne(ids)`; `Szukaj` pomija warianty rezerwy, bo psułyby próbną maturę.
- Klucze `Znajdz`/`Szukaj`: fragmenty tagów z `Wymiary('A')` (np. `'Kombinatoryka'`, `'Granica ciągu'`); „Nie rozpoznano wymiaru” → `Wymiary()`.
- Zadania wybieraj przez `Propozycja`, `Szukaj`, `Blizniak`, nie gołe `Znajdz`/`ZadaniaPodobne`: tylko one pomijają rezerwę i warianty, zadania sprzed 2015, zamknięte, śmieci OCR, rekordy bez punktów, odrzucone, duplikaty zrobionych i zadania z niesprawdzonych arkuszy. Wyniki zapisuj przez `Zapisz`, nie `Odnotuj`.
- Tabela `latwosc` (176 zadań CKE + 38 bliźniaków) i `WagaMatury` to dodatek: bez nich system działa jak przedtem. Łatwości kraju nie drukuj na arkuszu; uczeń widzi ją w `WYNIK.md`.
- Daty w czasie polskim (`Dzis()`), nie UTC.
- Arkusz jest wysłany dopiero, gdy w folderze leży `WYSLANO.txt`; sam `arkusz.pdf` nic nie znaczy. `sprzataj` nie rusza arkuszy bez `WYSLANO.txt`, bazy, `ANALIZA.md`, `WYNIK.md`, `zadania.json`, `raport.pdf`, `dane.json`.
- Dane od ucznia idą do `kontekst.json` i `rozgrzewka.json`, nie do `zadania.json`; klucz rozgrzewki jest tylko w `zadania.json` → `rozgrzewka`. `Wynik` nadpisuje `WYNIK.md`: po nim powtórz `petla bramki`.
- Skład PDF: `trim` zostawia krótki, wyrównany do lewej ostatni pas pod rysunkiem („Udowodnij, że …”), wyśrodkowane (BRUDNOPIS, „Strona x z y”) odpadają. Kod wyjścia 2 z `arkusz` = tusz przy dolnej krawędzi. Obrazy idą przez `drawImage`, nie `drawInlineImage` (inline psuje się w przeglądarkach mobilnych).
- Folder systemu to dysk sieciowy współdzielony przez wątki: przed edycją `.md` przeczytaj go ponownie i zmieniaj tylko swój fragment. `Wydluz` odmawia, gdy faza arkuszy ma już 4 tygodnie.
- Znany błąd: `arkusz` bywa, że ucina znak na końcu wiersza (np. „=” albo „−” przy prawej krawędzi kadru), choć obraz źródłowy jest dobry. Przy kontroli podglądu czytaj końce wierszy we wzorach; przy ubytku weź zapasowego kandydata.
