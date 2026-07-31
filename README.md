# Fund tracker

Estimerer daglig avkastning for fond som ikke publiserer den i tide.

Nordnet viser ikke gårsdagens avkastning for DNB Teknologi A før langt ut på
dagen. Men Nordnet viser *hva fondet eier*. Kombinerer man beholdningene med
sluttkursene til de underliggende aksjene, kan man regne seg fram til
avkastningen selv — og ha tallet klart kl. 22:15 samme kveld, et døgn før
fondet publiserer det.

## Modellen

```
avkastning = gjennomsnittlig avkastning i NOK for beholdningene
             × (1 − kontantandel)
             − daglig forvaltningshonorar
```

For hver beholdning:

```
avkastning_NOK = (kurs_i_dag / kurs_i_går) × (valutakurs_i_dag / valutakurs_i_går) − 1
```

Valutaleddet er ikke pynt. Fondet er i NOK, men eier Microsoft i USD, ADYEN i
EUR, Ericsson i SEK og Sony i JPY. USDNOK kan bevege seg 0,7 % på en dag — mer
enn de fleste aksjedager. Utelater man valuta, måler man feil ting.

## Hva modellen ikke vet

Tre antakelser, som er hele feilkilden:

1. **Beholdningene vi ikke ser oppfører seg som de vi ser.** Vektene
   normaliseres over det vi klarer å prise. `coverage_pct` i hvert estimat
   sier hvor stor del av fondet det faktisk er.
2. **Kontanter gir null avkastning på én dag.** Uproblematisk.
3. **Forvalter har ikke handlet siden snapshotet.** Umulig å vite utenfra, og
   grunnen til at estimatet forfaller etter hvert som beholdningsdataene eldes.

`backtest` finnes for å tallfeste akkurat dette — se under.

## Bruk

```bash
pip install -r requirements.txt
export PYTHONPATH=src

python -m fundtracker.cli funds                        # list fond
python -m fundtracker.cli resolve  dnb-teknologi-a     # hvilke navn mangler ticker
python -m fundtracker.cli snapshot dnb-teknologi-a     # hent og lagre beholdninger
python -m fundtracker.cli estimate dnb-teknologi-a     # dagens estimat
python -m fundtracker.cli backtest dnb-teknologi-a --days 250
python -m fundtracker.cli probe    dnb-teknologi-a     # test Nordnet-endepunkter
```

### Backtesten

Målet er ikke bare «hvor mye bommer modellen», men **hvor fort et
beholdnings-snapshot blir dårlig**. Vi har bare dagens sammensetning, så når
den kjøres bakover i tid blandes modellfeil og porteføljedrift med vilje.
Grupperer man feilen etter hvor langt tilbake dagen ligger, skiller de to seg:

- flat kurve → ferskhet betyr lite, det holder å scrape månedlig
- stigende kurve → hellingen sier hvor ofte det er verdt å hente nye data

## Oppsett

### Fondskonfigurasjon

Ett fond = én YAML-fil i `config/funds/`. Se
[`dnb-teknologi-a.yaml`](config/funds/dnb-teknologi-a.yaml). Nytt fond legges
til ved å kopiere fila og bytte ISIN, kostnad og ticker-tabell.

Ticker-tabellen må oppgi **noteringsvaluta**, ikke bare ticker. Et selskap med
flere noteringer — STMicroelectronics i Paris (EUR) og New York (USD) — har
nesten identisk kursutvikling, men helt ulikt valutaledd. Velger man feil
notering, blir estimatet systematisk skjevt.

Et navn uten ticker blir *rapportert*, aldri stille droppet.

### E-post

Sendes via SMTP med disse miljøvariablene, lagt inn som GitHub-secrets:

| Secret | Verdi |
|---|---|
| `SMTP_USER` | Gmail-adressen som sender |
| `SMTP_PASSWORD` | Gmail **app-passord** (ikke kontopassordet) |
| `MAIL_TO` | mottaker, standard er `SMTP_USER` |
| `SMTP_HOST` | valgfritt, standard `smtp.gmail.com` |
| `SMTP_PORT` | valgfritt, standard `587` |

App-passord krever at 2FA er slått på for Google-kontoen; det lages under
Google-konto → Sikkerhet → App-passord.

### Kjøreplan

`.github/workflows/daily-estimate.yml` kjører 20:15 og 21:15 UTC på hverdager.
GitHub-cron er alltid UTC, så begge kjører hele året for å treffe kl. 22:15
norsk tid uansett om USA har sommer- eller vintertid. Den andre kjøringen
avslutter uten å gjøre noe hvis dagens estimat allerede er logget.

`.github/workflows/diagnostics.yml` kjøres manuelt og er verktøyet for å teste
scraping og backtest mot ekte nett.

## Status

| Del | Tilstand |
|---|---|
| Avkastningsmodell med valuta og gebyr | Ferdig, dekket av tester |
| Beholdninger fra manuell CSV | Virker |
| Beholdninger fra Nordnet | Skrevet, endepunkt ikke verifisert — kjør `probe` |
| Kurser og valuta fra Yahoo | Skrevet, ikke verifisert mot ekte nett |
| NAV-historikk til backtest | Yahoo-ticker gjettet, må verifiseres |
| E-post | Skrevet, mangler secrets |

### Åpne spørsmål

- **Hvor mange beholdninger viser Nordnet egentlig?** Den manuelle fila har de
  22 øverste, til sammen 75,3 %. Resten mangler.
- **Kontantandel** fra «Fordeling»-boksen er ikke lagt inn.
- **Er fondet valutasikret?** Koden antar nei. Er det sikret, skal valutaleddet
  ut, og det snur fortegnet på en betydelig del av bevegelsen.
- **Når settes NAV?** Hele poenget med å kjøre 22:15 er at fondet prises på
  amerikansk stengetid. Er cut-off tidligere, må tidspunktet flyttes.
- **Hvilken notering** holder fondet av STM, SAP, Nokia, Sony og Samsung?
  Står i DNBs årsrapport.
