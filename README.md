<img src="docs/krawedz-gora.svg" alt="" width="100%">

<div align="center">

<br>

<img src="docs/naglowek.svg" alt="matura-tutor" height="80">

<p>Korepetytor AI do matury rozszerzonej z matematyki, działający w Claude Code.</p>

<a href="LICENSE"><img alt="Licencja MIT" src="https://img.shields.io/badge/licencja-MIT-0A0A0A?style=flat-square"></a>
<img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-0A0A0A?style=flat-square">
<img alt="4608 zadań CKE" src="https://img.shields.io/badge/zadania_CKE-4608-0A0A0A?style=flat-square">

</div>

<br>

<img src="docs/podglad.svg" alt="Arkusz ćwiczeniowy i dwie strony raportu tygodniowego wygenerowane przez matura.py" width="100%">

## Co robi

- **Dobiera zadania** z 4608 zadań CKE (2002–2026) pod luki ucznia.
- **Składa arkusze PDF** z rozgrzewką z własnych błędów.
- **Sprawdza zdjęcia rozwiązań** według zasad CKE, bez podawania gotowców.
- **Pamięta błędy** jako reguły „Jeśli …, to …” i wraca do nich, aż przestaną się powtarzać.
- **Co tydzień wydaje raport** z opanowaniem działów i prognozą wyniku.

## Start

```bash
git clone https://github.com/NelexstarMain/matura-tutor
cd matura-tutor
claude
```

Hook startowy sam instaluje zależności i tworzy bazę. Claude zadaje kilka pytań wstępnych i składa arkusz diagnostyczny.

## Struktura

```text
matura.py      kod i CLI
PROCEDURY.md   kroki lekcji, sprawdzania i raportu
CLAUDE.md      instrukcja dla Claude
.claude/       hook startowy i skill
docs/          dokumentacja techniczna
dane/          baza wzorcowa i harmonogram
baza_obrazy/   obrazy zadań
```

Szczegóły: [docs/DOKUMENTACJA.md](docs/DOKUMENTACJA.md)

## Wymagania

Python 3.10+, `poppler-utils`, do raportów TeX Live (`bash dane/instaluj_tex.sh`).

## Licencja

Kod na licencji [MIT](LICENSE). Zadania pochodzą z arkuszy CKE i nie są nią objęte. Dane ucznia zostają lokalnie i nie trafiają do gita.

<br>

<img src="docs/krawedz-dol.svg" alt="" width="100%">
