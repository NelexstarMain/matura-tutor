# Procedury systemu matura

Zasady, mapa folderu, CLI i spis funkcji: `README.md`. Polecenia uruchamiaj z głównego folderu repozytorium; w Pythonie `import sys; sys.path.insert(0, "<folder repo>"); import matura as m`. Krok 0 każdej procedury: `python3 matura.py kontrola` („BŁĄD” → „Gdy coś się nie uda”, „UWAGA” tylko odnotuj).

## Arkusz

Używasz, gdy uczeń prosi o arkusz (albo godzinę przed lekcją, jeśli ustawił to jako zadanie cykliczne); ta sekcja i `README.md` wystarczą.

Ustalenia stałe:
- Arkusz trafia do ucznia jako PDF w rozmowie (bez maili i stron WWW, chyba że uczeń o to poprosi).
- Etykieta wątku, tytuł PDF i wiadomości zawsze z dniem tygodnia i pełną datą: „Arkusz śr 21.10.2026 16:30” (pon, wt, śr, czw, pt, sob, nd; czas polski), `--tytul "Lekcja śr 21.10.2026, 16:30"`. Folder `RRRR-MM-DD_GGMM` (godzina lekcji; we wtorek i czwartek są dwie).

### Kroki

1. **Czy arkusz już jest.** Załatwiony tylko, gdy w folderze jest `WYSLANO.txt`. Jest `arkusz.pdf` bez znacznika: nie generuj od nowa, wyślij istniejące `arkusz.pdf` + `ANALIZA.md` (odśwież analizę, jeśli doszły wyniki) i zapisz `WYSLANO.txt` (data i godzina wysłania, nazwa wątku).
2. **Lekcja.** Uczeń prosi o arkusz = lekcja jest. Jeśli ma kalendarz i podłączone narzędzia, możesz sprawdzić, czy lekcja nie jest odwołana; kalendarza nie zmieniaj.
3. **Analiza postępów** → `arkusze/RRRR-MM-DD_GGMM/ANALIZA.md` (opis niżej). Przed drugą lekcją dnia uwzględnij wyniki pierwszej; nie powtarzaj jej zadań.
4. **Tydzień**: `t = m.Tydzien()`. `t["pelny"]` i dziś dzień pełnego arkusza (`m.Harmonogram()["pelny_arkusz"]["dzien"]`, domyślnie wtorek) → „Pełny arkusz próbny” niżej; inaczej arkusz codzienny.
5. **Plan**: `r = m.Propozycja("RRRR-MM-DD", "GG:MM", folder="RRRR-MM-DD_GGMM")` stosuje reguły niżej i dopasowanie z analizy, zapisuje `plan_arkusza.json`. Do `ANALIZA.md` dopisz `r["decyzje"]` i `r["tabela"]` (nr · rola · klucz · id · length · zapas). Czego `Propozycja` nie wie, popraw w slocie ręcznie i opisz w `ANALIZA.md`.
   - `blok`: klucze z `t["klucze"]`, najpierw ten z najmniejszą liczbą zadań w tym tygodniu; `length` z `t["length"]` (dolna połowa w 2 pierwszych lekcjach nowego działu i po serii fail, górna po > 85%). Faza 2: zadania bloku do 30% łatwości kraju, po trzech fail z rzędu powyżej 50%.
   - `powtorki`: najstarsze z `m.Powtorki()` (kolejność wg `priorytet`); dla każdej `m.Blizniak(pierwotne)`; zapamiętaj parę bliźniak:pierwotne.
   - `luki` (fazy 3–4): najsłabsze działy z analizy ważone punktami traconymi na maturze (waga × (1 − opanowanie)), `length` z górnej połowy; „+1 zadanie” po bloku tylko dla działu wartego ≥ 2 pkt (`WAGA_PLUS1`).
   - `mieszane`: dział z wcześniejszego bloku (`m.Harmonogram()["tygodnie"]`), średnia trudność; w fazach 3–4 kolejność wg punktów traconych.
   - Diagnoza (faza 0): po jednym zadaniu z 6 różnych kluczy bez prób; w 2 tygodnie każdy z 13 kluczy po 2 razy.
   - Przerwa świąteczna: tylko powtórki i mieszane. Brak historii: pomiń powtórki i luki, uzupełnij blokiem.
6. **Kandydaci**: slot ma 4 (`kandydaci`, pierwszy to wybór). `Szukaj` woli kadry spoza `dane/wadliwe_obrazy.json` i pomija zamknięte, śmieci OCR, rezerwę z wariantami, odrzucone. Ręcznie: `m.Szukaj(klucz, length=..., samples=4, exclude=wybrane)`.
7. **Kontrola obrazów**: obejrzyj obraz każdego wybranego (`m.Pobierz(id, 'photos')`, Read). Odrzuć: fragment rozwiązania lub klucza, wstęp strony, ucięte wzory lub ułamki, odwołanie do brakującego rysunku, treści usunięte z wymagań. Podpunkt (`_2`, `_3`, „Zadanie 11.2”) zależny od wspólnej treści: dołóż wcześniejszą część albo weź inne zadanie. Odrzucony: `m.Odrzuc(id, "powód")` i następny kandydat z „Zapas”.
8. **Reguły na górę arkusza** (`--regula`, do 3): z `notatki/dziennik_bledow.md` najpierw „Stałe reguły” typów spoza trzech bramek (warunki, niedokończone i kontrola już są w „Objaśnieniach” i metryczkach), potem najświeższe z typów, które się powtarzają, w formie „Jeśli …, to …”. Bez błędów w dzienniku: bez reguł.
9. **Rozgrzewka**: sekcja `## Rozgrzewka`, „Przed lekcją”; `pytania.json` przed złożeniem PDF. Pełny arkusz: bez rozgrzewki.
10. **PDF**:
    ```bash
    python3 matura.py arkusz --ids 113 1171 ... --out arkusze/RRRR-MM-DD_GGMM/arkusz.pdf \
      --tytul "Lekcja śr 21.10.2026, 16:30" --czas 75 --regula "..."
    ```
    Przycina kratkę, stopki i tabelki egzaminatora, wypisuje punkty (gdy są w treści), zapisuje `zadania.json` (nr, id, pkt, powt, rola, klucz, `rozgrzewka`, `nr_dokumentu`, `pola`, `bramki`; powtórki i role z `plan_arkusza.json`, `--powt` tylko przy ręcznym doborze) i `podglad/strona-N.png`. Kod wyjścia 2 = tusz przy dolnej krawędzi (wypisuje też zapasowych kandydatów): obejrzyj `kontrola/<id>_dol.png`; śmieć (kratka, stopka, tabelka) → ponownie z `--akceptuj <id>`, ucięta treść → `m.Odrzuc` i zapasowy kandydat.
    Układ formularza składa polecenie samo (nie dodawaj pól ręcznie): nagłówek z numerem `ARK-RRRR-MM-DD-GGMM`; A rozpoczęcie lekcji (godzina, sen, zmęczenie 1–5); B rozgrzewka (same pytania z nazwą formy i polem „Odpowiedź”; klucz tylko w `zadania.json`); C objaśnienia (trzy bramki, oznaczenia, reguły); D zadania z dwoma paskami pól (Plan · Start · Pomoc; Pewność · Kontrola · Utknąłem); E zakończenie (godzina, zmęczenie, najtrudniejsze, pytanie do korepetytora); stopka „Strona x z y”. `--bez-pol` i `--bez-bramek` tylko na wyraźną prośbę ucznia. „Rozgrzewka pominięta” = brak poprawnego `pytania.json`: popraw i złóż ponownie; gdy się nie da, arkusz bez rozgrzewki jest ważny (nie wracaj do starych „Jeśli …, to …?”), odnotuj w `ANALIZA.md`.
11. **Podgląd**: obejrzyj każdą stronę `podglad/strona-N.png` (tekst w komórkach, także całe pytania rozgrzewki; nic poza ramkami i marginesem); wymień wadliwe zadania. Nie wysyłaj, dopóki każda strona nie jest czysta.
12. **Wiadomości o postępach** (zwykłe wiadomości, bez pliku LOG.md): 1–3 wiersze, same słowa kluczowe, po analizie, po doborze zadań i po złożeniu PDF. Każda: **Wykryto** (co wynika z danych ucznia, np. „błędy w prawdopodobieństwie warunkowym, 3 z 4 nieudane”), **Wybrano** (dział lub typ i dlaczego), **Komenda** (treść i skrócony wynik, np. `m.Szukaj("trygonometria", length=2.5)` → 4 kandydatów, id 3087 wybrane). Nie powtarzaj odpowiedzi końcowej. Pytań rozgrzewki, kluczy i reguł wybranych fiszek nie pisz w żadnej wiadomości ani w `ANALIZA.md` (wystarczy „rozgrzewka: 5 pytań, kontrola OK”).
13. **Wysyłka**: odpowiedź w rozmowie: ile zadań, 2–3 zdania z analizy (co dobrze, co słabo, z czego arkusz), dołącz `arkusz.pdf` i `ANALIZA.md`. Część 2 pełnego arkusza: jedna linijka z numerami zadań i ten sam `arkusz.pdf`. Na prośbę w rozmowie: 1–2 linijki i `arkusz.pdf`. Zaraz potem zapisz `WYSLANO.txt`.
14. **Gdy coś się nie uda** (błąd kontroli, brak folderu, zły PDF): napisz to wprost uczniowi.
15. **Sprzątanie** na końcu: `python3 matura.py sprzataj` (próba na sucho), gdy lista ma tylko stare pliki: `... --usun`. Domyślnie > 14 dni: znikają `arkusz.pdf` i `podglad/` starych arkuszy, wykresy, podglądy i `.tex` starych raportów, symulacje i atlasy w całości. Zostają `matura.db`, `ANALIZA.md`, `WYNIK.md`, `zadania.json`, `raport.pdf`, `dane.json`; arkuszy bez `WYSLANO.txt` nie rusza.

Po wysłaniu czekaj na zdjęcia rozwiązań ucznia (nawet po dniach) i wykonaj wtedy `## Sprawdzanie`.

### Analiza postępów

Także na pytanie „jak mi idzie?”, w pierwszym arkuszu tygodnia i na końcu fazy.
1. **Dane**: `print(m.Raport())` (tydzień planu, liczby prób, wynik z 14 dni, typy błędów, `m.Profil()`: opanowanie działów w % z 80% przedziałem, mocne / słabe / za mało danych, najsłabsze umiejętności; linia „Na tle kraju”, prognoza z centylem `m.Prognoza`, pewniaki z za małą liczbą prób, zaległe powtórki, niesprawdzone arkusze, ścieżka poprzedniej `ANALIZA.md`). Szczegóły: `m.Profil()["dzialy"]`, `m.Ostatnie(30)`. Przeczytaj też `notatki/dziennik_bledow.md` (2 tygodnie), `notatki/postep.md` (ostatni pełny arkusz, najbliższy próg) i poprzednią `ANALIZA.md`.
2. **Wnioski**, każde z liczbą z bazy:
   - Mocne: lista „Mocne” (dolna granica przedziału ≥ 75%); dział z „za mało danych” nie jest mocny.
   - Luki: lista „Słabe” (górna granica < 55% albo wynik < 55% przy ≥ 3 próbach), potem najsłabsze umiejętności; przerobione działy bez prób to też luki; „za mało danych” sprawdź w arkuszu.
   - Typy błędów: który się powtarza i jaka reguła z dziennika działa; z „Bramki i kontekst” w `WYNIK.md`: pewne błędy, kalibracja, plan, kontrola, rozgrzewka.
   - Kontekst: tempo min/pkt wobec 3,6, gdzie utyka, czy błędy rosną ze zmęczeniem lub po krótkim śnie (tylko opis; dobór wg kroku 5).
   - Trend: zmiana względem poprzedniej analizy i odległość od progu fazy.
3. **Dopasowanie** (zapisz w `ANALIZA.md`, co zmieniłeś; kolejność bloków zmienia tylko przegląd planu):
   - Dział „słaby” po swoim bloku → +1 zadanie z niego w każdym arkuszu, aż przestanie być słaby (`Propozycja` robi to sama w fazach 3–4).
   - Skuteczność > 8,5 przez tydzień → `length` +0,5; seria fail → −0,5.
   - Więcej niż 4 zaległe powtórki → 3 powtórki zamiast 2.
   - Niesprawdzony arkusz: jego zadania są pominięte w doborze; po 7 dniach napisz uczniowi raz, że czekają; sam nie wpisuj fail.
   Kryterium działu do „+1 zadania” to status „słaby” z `Profil` i `WAGA_PLUS1`.
4. **Zapis**: `ANALIZA.md` do ok. 40 linii: Liczby · Mocne strony · Luki · Typy błędów · Powtórki i niesprawdzone · Postęp do progu · Plan arkusza. Gdy uczeń pytał: odpowiedź w 3–5 zdaniach i link. W analizie nie wywołuj `Odnotuj` ani `Zapisz`.

### Pełny arkusz próbny

1. `Propozycja` daje w ten dzień `typ: "pelny"` (pierwsza lekcja) albo `"pelny_czesc_2"` (druga), z `ids` zestawu i `podzial` (część 1 do połowy punktów, np. zad. 1–7 = 23 pkt w CKE_2025_Z01). `m.Zestaw(t["pelny"])`: suma `pkt` = 50 (sprawdza też `kontrola`). Rekordy `_1` to dalsze strony zadania: zostaw w kolejności.
2. Obejrzyj obraz każdego rekordu (jak w kroku 7); ucięty kadr rozwiąż przez `--akceptuj` albo opisz w `ANALIZA.md`, nie wymieniaj zadań zestawu. Matury z rezerwy (`CKE_2025_05`, `CKE_2026_05`): opcje `--akceptuj`/`--rysunek` podaje `decyzje`; łatwości kraju nie drukuj.
3. `python3 matura.py arkusz --ids <wszystkie id po kolei> --out arkusze/<folder pierwszej lekcji>/arkusz.pdf --tytul "Arkusz próbny DD.MM (zestaw ...)" --czas 180 --rozgrzewka 0` (bez rozgrzewki jak na egzaminie; pola i metryczki zostają).
4. Podział według `m.Podzial(zestaw)`: pierwsza lekcja część 1, druga reszta. Na drugiej lekcji tego dnia nie składaj nowego arkusza: przypomnienie z numerami zadań części 2 i tym samym PDF.
5. Nie wywołuj `Zapisz`: wyniki zapisuje sprawdzanie (też do `notatki/postep.md`).

### Zasady doboru i uczenia

- Przy tworzeniu arkusza nie wywołuj `Odnotuj`/`Zapisz`. Żadne zadanie dwa razy w arkuszu (`exclude`); zadania z niesprawdzonych arkuszy i z rezerwy pełnych arkuszy pomija `Szukaj`.
- Od razu zadania; teoria tylko przy błędzie, jako karta metody w `notatki/karty/`. Sokratycznie: jedno pytanie naprowadzające, pełne rozwiązanie na prośbę.
- Mieszanie: najwyżej połowa arkusza z bloku tygodnia. Nowy dział: pierwsze 2 lekcje bloku i dolna połowa zakresu `length`.
- Powtórki: `m.Powtorki()` daje zadania z błędem sprzed 3 (fail), 7 (partial) lub 21 dni (po udanej powtórce); do arkusza bliźniak.
- Trudność: cel 60–80% skuteczności. Po fail łatwiej (`length` −0,5 albo `tryb='easier'`), po ok trudniej (+0,5 albo `'harder'`).
- Informacja zwrotna tego samego dnia; pilnuj nawyków egzaminacyjnych: dziedzina na początku, każdy krok zapisany, odpowiedź na pytanie z treści, uzasadnienie ekstremum.
- Trzy bramki: przed liczeniem plan w jednej linijce (Cel · Droga · Ramka) i reguła 2 minut bez postępu → droga B; przy ryzykownej operacji najpierw warunek; przed oddaniem kontrola 4 kroków (W · P · S · O); pewność 1–3 przy każdym zadaniu. Po sprawdzeniu: pewne błędy najpierw, reguła „jeśli–to” w dzienniku i jako fiszka, rozgrzewka. Na początek: niedokończone ≤ 8% i warunki ≤ 3% punktów arkusza (dotąd 15% i 6%).
- Pola od ucznia (godziny, sen, zmęczenie, pewność, kontrola, utknięcie, pomoc, najtrudniejsze, pytanie) sprawdzanie zapisuje jako tagi w bazie i w `kontekst.json`.

## Sprawdzanie

Używasz, gdy uczeń przysyła zdjęcia rozwiązań.

Pytania zadawaj pojedynczo, wyjaśnienia tylko przy błędach, życzliwie i konkretnie, wzory w LaTeX.

1. **Arkusz.** Folder wskazany przez ucznia albo najnowszy z `m.Niesprawdzone()`. Z `arkusze/<folder>/zadania.json`: nr → id, `pkt` (maks.), `powt` (id pierwotnego zadania), `rozgrzewka` (pytania 1–5: `fiszka`, `forma`, `kontekst`, `pytanie`, `klucz`), `pola`, `bramki`. Dopasuj zdjęcia do „Zadanie N”; treść czytaj z obrazu. Nie zmieniaj `zadania.json` (`m.Wynik` liczy próby od daty w nazwie folderu minus 3 h); dane od ucznia idą do `kontekst.json` i `rozgrzewka.json`.
2. **Rozwiąż sam i sprawdź kodem** (sympy/numpy) przed oceną; nie oceniaj z pamięci, nie ufaj polu `solution`.
3. **Punkty jak CKE.** Maks. z `pkt`; brak → typowa waga (2 pkt krótkie, 3–4 średnie, 5–6 optymalizacja lub dowód) i w notatce „maks. szacowane”. Cząstkowe: postęp, istotny postęp, pokonanie zasadniczych trudności, rozwiązanie z usterką, pełne. Odejmuj za: brak dziedziny lub założeń, wynik bez obliczeń, brak uzasadnienia ekstremum, odpowiedź nie na pytanie z treści.
4. **Typ błędu** (jeden główny): `rachunkowy`, `pojęciowy` (wzór, definicja), `metoda`, `warunki` (dziedzina, założenia, przypadki), `niedokończone`, `odczyt` (treść), `zapis` (brak kroków, uzasadnienia, odpowiedzi).
5. **Pola z arkusza** (jedyne dane o tym, jak uczeń rozwiązywał). Odczytaj wszystko, co wpisał:
   - Rozpoczęcie lekcji: godzina, sen (h), zmęczenie 1–5.
   - Pod zadaniem: Plan (cel · droga · ramka), Start (godzina), Pomoc (notatki, internet, AI); Pewność 1–3 (1 zgaduję, 2 raczej, 3 pewne); Kontrola W · P · S · O (wzory z tablic, podstawienie, sens, odpowiedź); Utknąłem: start (brak pomysłu), droga (w połowie), rachunki (w liczeniu), czas (czas lub zmęczenie).
   - Zakończenie: godzina, zmęczenie 1–5, najtrudniejsze, pytanie do korepetytora.
   - Rozgrzewka: odpowiedzi w kolumnie „Odpowiedź” (bez wydruku: na kartce jako 1–5); klucza na arkuszu nie ma.
   Bez wydruku uczeń pisze skrótem przy zadaniu, np. „14:07 · p3 · WPSO · utk droga”. Puste pole planu lub kontroli przy wypełnionej reszcie metryczki = „nie” (`[plan:nie]`, `[kon:-]`); cała metryczka pusta = brak tagów, nie zgaduj. Plan liczy się też, gdy Cel · Droga · Ramka stoi na początku rozwiązania. Brak zdjęcia 1. lub ostatniej strony: poproś raz, nie wstrzymuj sprawdzania.
   Minuty: `python3 matura.py petla czasy 1:14:05 3:14:20 2:14:41 … --koniec 15:15` (kolejność rozwiązywania z godzin startu, ostatnie do końca) → `[min:N]`.
6. **Wskazówka, nie gotowiec.** Najpierw pewne błędy (pewność 3 bez kompletu): zapytaj, co dawało pewność. Potem błędy w zadaniach łatwych dla kraju (≥ 60%, lista „Najpierw omów” w „Na tle kraju” w `WYNIK.md`). Przy błędzie jedno pytanie naprowadzające wskazujące miejsce („Spójrz na trzecią linijkę: jaki warunek musi spełniać mianownik?”) i krótko teoria, której zabrakło. Utknięcie: „start” → pierwszy krok i plan, „droga” → droga B, „rachunki” → konkretna linijka, „czas” → tempo, nie wiedza. Zaznaczona kontrola przy złym wyniku (np. P, a podstawienie obala wynik) to kontrola „na niby”: jednym zdaniem pokaż, co by wyłapała. Zmęczenie 4–5 lub mało snu przy błędach rachunkowych zapisz jako możliwą przyczynę, nie wymówkę. Pełne rozwiązanie na prośbę. Poprawa po wskazówce nie zmienia punktów; `pomoc=True` tylko gdy uczeń zaznaczył „Pomoc” albo prosił o pomoc w trakcie.
7. **Zapis w bazie**, jedno `Zapisz` na zadanie na lekcję (kolejne to nowa próba); tagi z pól na początku opisu w tej kolejności: `[pew:N] [min:N] [utk:start|droga|rachunki|czas] [plan:tak|nie] [kon:WPSO]` (`[kon:-]` = żaden krok; tag pomijasz, gdy pola nie było albo było puste wg reguły z 5):
   ```python
   m.Zapisz(id, pkt=3, maks=5, typ="warunki", opis="[pew:3] [min:14] [plan:tak] [kon:PS] pominął x>0 w logarytmie", powt=zadanie.get("powt"))
   m.Zapisz(id, pkt=4, maks=4, opis="[pew:2] [min:9] [plan:tak] [kon:WPSO] czysto")   # komplet
   m.Zapisz(id, pkt=0, maks=5, typ="niedokończone", opis="[pew:1] [min:22] [utk:start] [plan:nie] [kon:-] brak pomysłu na parametr")
   ```
   `ok` = komplet bez pomocy, `fail` = 0 pkt, reszta `partial`. Tagi to słowa ucznia: nie dopisuj niezaznaczonych; twoja ocena planu i kontroli idzie tylko przez `--plan`/`--kontrola` w kroku 11. Po zapisie sprawdź `m.Ostatnie(n)`; pomyłkę w ocenie popraw przez `m.Popraw(id, pkt=…, maks=…, typ=…, opis=…)` (ostatni zapis zadania), nie drugim `Zapisz`. Zadanie niewysłane: nie zapisuj, zapytaj raz. Nieczytelne zdjęcie: zapytaj. Zaczęte, nieskończone: punkty za to, co jest, typ `niedokończone`.
8. **Dziennik błędów** (`notatki/dziennik_bledow.md`, przeczytaj tuż przed edycją): nowy wiersz na górze tabeli „Błędy”: data, folder arkusza, id, dział (`m.Dzial(id)`), typ, co się stało, reguła „Jeśli <sygnał w treści albo rachunku>, to <odruch>.” (np. „Jeśli dzielę przez wyrażenie z x, to wyłączam je przed nawias.”). Trzeci błąd tego samego typu w 2 tygodnie → reguła także w „Stałe reguły”.
9. **Fiszka** (`notatki/karty/reguly.md`): jest fiszka na ten błąd (`python3 matura.py petla lista`) → `python3 matura.py petla wroc NR`; inaczej `python3 matura.py petla dodaj "<sygnał bez słowa jeśli>" "<odruch>" --przyklad "id X (DD.MM): co się stało" --typ <typ> --dzial "<dział>"`. Bez fiszki tylko błędy jednorazowe bez ogólnej reguły (np. zmęczenie).
10. **Rozgrzewka i kontekst.** Odpowiedzi porównaj z `klucz` (liczy się „Zalicz, gdy …”, nie brzmienie): `python3 matura.py petla ocen RRRR-MM-DD_GGMM 1:tak 2:nie 3:tak 4:tak 5:tak` (`--popraw` poprawia ocenę); bez odpowiedzi nie oceniaj, przypomnij o nich jednym zdaniem. Kontekst lekcji: `python3 matura.py petla kontekst RRRR-MM-DD_GGMM --start 14:02 --koniec 15:15 --sen 7 --zm-start 2 --zm-koniec 4 --najtrudniejsze "…" --pytanie "…"` (tylko wypełnione pola, `kontekst.json`, kolejne wywołanie uzupełnia). Na „Pytanie do korepetytora” odpowiedz w kroku 13 pytaniem naprowadzającym albo krótkim wyjaśnieniem.
11. **WYNIK.md**: `m.Wynik("RRRR-MM-DD_GGMM")` (tabela nr · id · punkty · kraj · typ błędu, suma, procent, typy, zadania bez wpisu, „Na tle kraju”: punkty wobec przeciętnego maturzysty, błędy w zadaniach łatwych dla kraju, przy prawdziwej maturze wynik wobec średniej kraju, centyl przy arkuszu 50 pkt, prognoza matury). Zaraz potem `python3 matura.py petla bramki RRRR-MM-DD_GGMM` dopisuje „Bramki i kontekst” (pewność, minuty, utknął, plan, kontrola; pewne błędy, kalibracja, tempo wobec 3,6 min/pkt, utknięcia, kontekst, rozgrzewka). Gdy plan lub kontrola nie były wpisane, a z rozwiązania widać, czy były, dodaj własną ocenę `--plan 1:tak 2:nie … --kontrola 1:tak 2:nie …` (tylko zadania bez tagu). `Wynik` nadpisuje cały plik: po każdym jego uruchomieniu powtórz `petla bramki`. Pod spodem 2–3 powtarzające się problemy własnymi słowami.
12. **Karta metody**: pierwszy błąd `pojęciowy` lub `metoda` w dziale bez karty w `notatki/karty/` → karta według formatu w `README.md` (Mapa folderu), dołączona do odpowiedzi; istniejącą uzupełnij.
13. **Pełny arkusz**: wiersz w `notatki/postep.md` → „Pełne arkusze” (data, zestaw, punkty /50, %, czas, dział z największą stratą) i porównanie z progiem fazy (tabela „Progi faz”). Próg niespełniony: napisz to w odpowiedzi; decyzje o planie podejmuje przegląd.
14. **Odpowiedź** dla ucznia, krótko: wynik (np. 31/40 pkt, 78%) i zdanie na tle kraju (szacunek oznacz „ok.”); pewne błędy; błędy w zadaniach łatwych dla kraju; 2–3 błędy z regułami „jeśli–to”; wynik rozgrzewki jednym zdaniem (przy każdym „nie” poprawna odpowiedź i reguła: tu uczeń pierwszy raz widzi klucz); zdanie z pól, jeśli coś wynika (tempo, utknięcie, zmęczenie); odpowiedź na jego pytanie; co wróci jako powtórka. Dołącz kartę metody, jeśli powstała. Odpowiadaj tylko w tym wątku; przed odpowiedzią lista statusu ✓.

## Rozgrzewka

Używasz przy składaniu arkusza (krok 9 sekcji `## Arkusz`) do ułożenia pytań oraz po lekcji do ich oceny.

Pięć krótkich pytań, razem 5 minut na początku lekcji. Każde każe użyć jednej reguły z dziennika błędów (fiszka w `notatki/karty/reguly.md`), ale za każdym razem w innej sytuacji: inna forma, dział, liczby. Powtórki w odstępach (1, 3, 7, 21, 28 dni) dotyczą reguły, nie zdania na kartce; uczeń ćwiczy rozpoznawanie sygnału w nowym zadaniu. Odpowiedzi nie ma na arkuszu; klucz uczeń widzi po sprawdzeniu.

### Jak to działa

1. `python3 matura.py petla rozgrzewka FOLDER` wybiera 5 fiszek na powtórkę (zapomniane najpierw) i podaje formy do wyboru oraz trzy ostatnie pytania każdej.
2. Sesja arkusza układa nowe pytanie do każdej i zapisuje je z kluczem w `pytania.json` w folderze arkusza.
3. `python3 matura.py petla pytania FOLDER` sprawdza format i powtórzenia.
4. `matura.py arkusz` drukuje same pytania z polem na odpowiedź, klucz zapisuje w `zadania.json` → `rozgrzewka`, pytania w dzienniku `notatki/karty/pytania.md` (kontrola powtórek stoi na nim).
5. Po lekcji sprawdzanie porównuje odpowiedzi z kluczem; `petla ocen` ustawia odstęp fiszki (zapomniana wraca po 1 dniu) i wpisuje wynik do dziennika.

### Formy pytań

Nad pytaniem na arkuszu stoi nazwa formy; reguły nie zdradza nic.

| Kod | Na arkuszu | Co robi uczeń | Klucz zawiera | Przykład |
| --- | --- | --- | --- | --- |
| `blad` | Znajdź błąd | wskazuje linijkę z błędem i poprawia ją | miejsce błędu, poprawny wynik, „Zalicz, gdy …” | x³ = 4x ⇒ x² = 4 ⇒ x = −2 lub x = 2. Znajdź błąd. |
| `mini` | Mini-zadanie | rozwiązuje zadanie na minutę, w którym reguła decyduje o wyniku | wynik i miejsce pułapki | Rozwiąż nierówność x² > 49. |
| `warunki` | Najpierw warunki | wypisuje dziedzinę, warunki albo przypadki, bez rozwiązywania | pełną listę warunków | Równanie (m − 1)x² + 2x + m = 0 ma dwa różne pierwiastki dodatnie. Wypisz same warunki na m. |
| `plan` | Tylko plan | pisze Cel · Droga · Ramka (albo drogę A i drogę B), bez liczenia | elementy, bez których plan jest zły | Wykaż, że x⁴ − 4x + 3 ≥ 0 dla każdego x ∈ ℝ. Zapisz tylko tezę i drogę. |
| `pf` | Prawda czy fałsz | pisze P albo F i jedno zdanie uzasadnienia lub kontrprzykład | P/F i uzasadnienie | Jeśli f′(2) = 0, to f ma ekstremum w x = 2. |
| `wynik` | Sprawdź wynik | sprawdza podany wynik bez liczenia od nowa: podstawienie, sens, x = 0, rząd wielkości | czy wynik jest dobry i jak to widać | Z 10 osób losujemy 3. Wynik: szansa, że wylosujemy Anię, to 3/1000. Sprawdź. |
| `sygnal` | Gdzie pułapka | wskazuje miejsce, w którym łatwo o błąd, i co tam zrobić, bez rozwiązywania | sygnał i odruch | Rozwiąż równanie cos(x/3) = 1. Gdzie łatwo o błąd? |

Przykłady z tej tabeli nie trafiają na arkusz (`petla pytania` odrzuca podobne).

Dopasowanie formy do typu fiszki (`FORMY_DLA_TYPU`; forma ma zmuszać do użycia reguły, nie do jej wyrecytowania):

| Typ fiszki | Formy |
| --- | --- |
| warunki | blad, mini, warunki, pf, wynik |
| pojęciowy | blad, pf, mini, wynik |
| rachunkowy | wynik, blad, mini |
| niedokończone | plan, sygnal, mini |
| kontrola | wynik, blad, pf |
| odczyt | sygnal, mini, blad |
| zapis | blad, pf, mini |
| metoda | plan, sygnal, blad |

### Zasady: nigdy to samo

1. Pytanie jest nowe: niepodobne do żadnego z dziennika ani do przykładów wyżej (próg 80% wspólnej treści, `PODOBNE`). Same nowe liczby to za mało.
2. Forma inna niż w dwóch ostatnich pytaniach fiszki (`petla rozgrzewka` podaje, z czego wybierać).
3. Kontekst inny niż ostatnim razem: inny dział (np. reguła o dzieleniu przez wyrażenie z x: raz trygonometria, raz wielomiany, raz logarytmy); jeśli pasuje, dział z bieżącego bloku (`m.Tydzien()["klucze"]`) lub z ostatnich lekcji.
4. Na arkuszu co najmniej 3 różne formy i najwyżej 2 pytania z jednego działu.
5. Trudność rośnie z serią fiszki. Seria 0 (świeża lub zapomniana): sygnał widać od razu. Seria 1–2: sygnał w krótkim zadaniu z innego działu. Seria 3+: sygnał ukryty w zadaniu jak z matury.
6. W `wynik` i `pf` mniej więcej co drugi raz wynik lub zdanie jest poprawne, żeby uczeń sprawdzał, a nie zgadywał „pewnie źle”.
7. Jedno pytanie = jedna reguła; odpowiedź w 1–2 linijkach, do minuty.
8. Pytanie nie podpowiada reguły („pamiętaj o dziedzinie” zakazane) i nie przepisuje błędu z kolumny „Przykład” fiszki (to źródło reguły, nie pytanie).
9. Klucz kończy się kryterium „Zalicz, gdy …”.

### Przed lekcją (sesja arkusza)

1. `python3 matura.py petla rozgrzewka RRRR-MM-DD_GGMM`: dla 5 fiszek reguła, typ, seria, formy, ostatnie pytania.
2. Do każdej fiszki jedno pytanie według zasad. Treść do 260 znaków, wzory w Unicode (x², √(x − 1), log₂ x, sin²x, ≤, ≠, π, ∈, ℝ), bez LaTeX-a. Linijki rozwiązania w `blad` oddziel `\n`.
3. Rozwiąż każde sam; rachunek klucza sprawdź w sympy. W `blad` dokładnie jeden błąd, reszta poprawna.
4. Zapisz `arkusze/RRRR-MM-DD_GGMM/pytania.json`:
   ```json
   {"pytania": [
    {"fiszka": 2, "forma": "mini", "kontekst": "Wielomiany",
     "pytanie": "Rozwiąż równanie x(x − 3)² = 4(x − 3).",
     "klucz": "x ∈ {−1, 3, 4}: (x − 3)(x² − 3x − 4) = 0. Zalicz, gdy jest x = 3, czyli nie podzielił przez (x − 3)."}
   ]}
   ```
   Pola: `fiszka` (numer z `reguly.md`), `forma` (kod), `kontekst` (dział), `pytanie`, `klucz`.
5. `python3 matura.py petla pytania RRRR-MM-DD_GGMM`: każdy BŁĄD popraw (pytanie, forma lub kontekst) i uruchom ponownie; uwagi rozważ.
6. Złóż arkusz (`matura.py arkusz` bez `--rozgrzewka 0`). Dziennik pytań zapisuje się tylko przy prawdziwym składzie, nie przy `--test`; ponowny skład w tym samym folderze zastępuje jego wiersze.
7. Pytań i kluczy nie pisz w rozmowie, w wiadomościach o postępach ani w `ANALIZA.md`.

### Po lekcji (sesja sprawdzania)

1. Odpowiedzi: kolumna „Odpowiedź” w sekcji Rozgrzewka (bez wydruku: kartka, 1–5). Porównaj z `klucz` w `zadania.json` → `rozgrzewka`; liczy się „Zalicz, gdy …”.
2. `python3 matura.py petla ocen RRRR-MM-DD_GGMM 1:tak 2:nie 3:tak 4:tak 5:tak` (`--popraw` przy korekcie).
3. Dla ucznia: wynik jednym zdaniem; przy każdym „nie” poprawna odpowiedź i reguła, której dotyczyło pytanie (tu pierwszy raz widzi klucz). Bez odpowiedzi nie oceniaj (fiszki wrócą same), przypomnij jednym zdaniem.

### Gdy czegoś brak

- Brak `pytania.json` lub zły format: arkusz powstaje bez rozgrzewki (`matura.py arkusz` wypisuje uwagę), odnotuj w `ANALIZA.md`; nie wracaj do starego „Jeśli …, to …?”.
- Pełny arkusz próbny: `--rozgrzewka 0`. Dwie lekcje jednego dnia: druga dostaje inne fiszki sama.
- Fiszek do powtórki mniej niż 5: dobiera te z najbliższym terminem; uwaga „przypada dopiero …” jest wtedy w porządku.
- Nowa forma: dopisz w tabeli „Formy pytań” i w `FORMY`, `FORMY_DLA_TYPU` w `matura.py` (po kopii zapasowej). Liczba pytań: `petla rozgrzewka FOLDER -n N` (arkusz drukuje tyle, ile jest w `pytania.json`).

## Raport

Raport robisz na prośbę ucznia o raport lub podsumowanie tygodnia (albo raz w tygodniu, jeśli ustawił zadanie cykliczne).

Raport trafia do ucznia jako PDF w rozmowie. Raport tylko czyta bazę: niczego nie zapisuj przez `Odnotuj`/`Zapisz`, nie zmieniaj harmonogramu.

1. **Tytuł**: `Raport pt DD.MM.RRRR` (dzień tygodnia i pełna data), np. „Raport pt 23.10.2026”.
2. `python3 matura.py raport` (pierwsze uruchomienie w świeżym kontenerze instaluje TeX Live przez `dane/instaluj_tex.sh`, ok. 1–2 min; potem ok. 10 s). Wynik: `raporty/RRRR-MM-DD_tydzien/` z `raport.pdf`, `raport.tex`, `dane.json`, `wykresy/`, `podglad/strona-N.png`. `--dzien RRRR-MM-DD`: raport na dany dzień (okno 7 dni do 9:47).
3. Przeczytaj `dane.json`, `notatki/dziennik_bledow.md` (ostatni tydzień), `notatki/postep.md`, najnowszą `arkusze/*/ANALIZA.md`. Napisz `raporty/RRRR-MM-DD_tydzien/komentarz.md`: 3–5 zdań po polsku, jak korepetytor do ucznia, każde z liczbą z raportu: co dobrze, co najsłabsze i dlaczego (typ błędu), co zmienia się w przyszłym tygodniu. Zwykły tekst, akapity rozdzielone pustą linią, wzory w `$...$`. Brak prób w tygodniu: jedno zdanie, że raport jest pusty, i prośba o zdjęcia rozwiązań.
4. `python3 matura.py raport --komentarz raporty/RRRR-MM-DD_tydzien/komentarz.md` (PDF jeszcze raz, z ramką „Komentarz korepetytora”).
5. Obejrzyj każdą stronę `podglad/strona-N.png` (Read): tekst poza stroną, nachodzące etykiety, puste wykresy. Drobny problem odnotuj w odpowiedzi; brak PDF lub błąd pdflatex → krok 7.
6. Odpowiedź: 2–3 zdania (próby w tygodniu, średni wynik, najsłabszy dział, główna zmiana na przyszły tydzień) i `raport.pdf` (bez PNG i `.tex`).
7. **Gdy coś się nie uda** (brak pdflatex po instalacji, błąd polecenia, pusty PDF): napisz to wprost z ostatnimi liniami `raport.log`.
8. **Sprzątanie** na końcu: `python3 matura.py sprzataj`, potem `--usun` (> 14 dni; nie rusza bazy ani `raport.pdf`).

Test bez prawdziwych danych: `python3 matura.py raport --symulacja` (kopia bazy w katalogu tymczasowym, wynik w `raporty/RRRR-MM-DD_tydzien_sym/`). Inne rodzaje tylko na prośbę ucznia (nie są zaplanowane; opcje `--symulacja`, `--dzien`, `--out`): `raport-calosc` (cała historia, też `--komentarz`, wynik `_calosc`) i `raport-atlas` (atlas całej bazy z nałożonymi próbami, wykresy 3D, wynik `_atlas`). Rodzaj `metoda` to jednorazowy raport o redukcji błędów (23.10.2026), bez polecenia w `matura.py`.

## Przegląd planu

Robisz co 2 tygodnie albo gdy uczeń poprosi o przegląd planu.

Plan jest kolejką bloków, nie sztywnym kalendarzem; przegląd dopasowuje go do wyników ucznia. Najwyżej jedna zmiana na przegląd. To jedyne miejsce, gdzie wolno zmienić `dane/harmonogram.json`.

1. **Czy to tydzień przeglądu.** Dziś nie taki dzień albo w `notatki/postep.md` → „Przeglądy” jest wpis z ostatnich 10 dni → zakończ bez odpowiedzi. Po 9.05.2027 też.
2. **Dane** (brak scipy → `python3 -m pip install -q scipy`): `m.Przeglad()`, `m.Pokrycie()`, `m.Slabe(limit=5, min_attempts=2)`, `m.Tydzien()`, ostatni wiersz „Pełne arkusze” i progi w `notatki/postep.md`, typy błędów z 14 dni w `notatki/dziennik_bledow.md`. Przy ≥ 15 próbach także `m.Opanowanie()["raport_tekstowy"]`.
3. **Decyzja** (pierwsza pasująca reguła, stop):
   - Mniej niż 8 sprawdzonych zadań w 14 dni → bez zmian; napisz, ile arkuszy czeka na zdjęcia.
   - Blok z ostatnich 2 tygodni średnio < 6/10 (≥ 4 zadania z jego działu) → `m.Wydluz(tydzień_bieżący, powód)`: blok wraca na kolejny tydzień.
   - Najbliższy blok dotyczy działu, w którym uczeń ma ≥ 8,5/10 z ≥ 4 zadań → `m.Skroc(numer_tygodnia_tego_bloku, powód)`: blok wypada, faza arkuszy zyskuje tydzień.
   - Ostatni pełny arkusz ≥ 10 pkt poniżej progu fazy → `m.Wydluz` dla najsłabszego z ostatnich bloków.
   - Inaczej bez zmian.
   W fazach 3 i 4 bloków nie przesuwaj (zmieniają się tylko działy idące jako „luki”, robi to analiza przed arkuszem). `Wydluz` odmawia, gdy faza arkuszy ma już 4 tygodnie: wtedy bez zmian i napisz to wprost.
4. **Zapis**: wiersz w `notatki/postep.md` → „Przeglądy”: data, próby i średni wynik z 14 dni, decyzja (`Wydluz`/`Skroc` same dopisują „Zmiany planu”).
5. **Odpowiedź**, do 5 zdań: wynik z 14 dni (liczby), co dobrze i słabo, co zmieniłeś i dlaczego (albo że bez zmian), blok przyszłego tygodnia. Na końcu: „Napisz «cofnij», jeśli wolisz poprzedni plan.”
6. **„Cofnij”**: przywróć poprzednią treść tygodni (po `Wydluz` → `Skroc` tego samego tygodnia + 1, po `Skroc` → `Wydluz`), wpis w „Zmiany planu”.

Zmianę planu z rozmowy (ferie, odwołana lekcja, inny blok) wolno wykonać wprost na prośbę ucznia: `m.Wydluz` / `m.Skroc` (faza 3 to bufor, min. 4 tyg.), zapis w `notatki/postep.md`.
