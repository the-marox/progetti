# progetti
## Radar Porcini

Dove e quando cercare funghi in Toscana: https://the-marox.github.io/progetti/radar-porcini/

Il sito sta nel ramo `gh-pages`, cartella `radar-porcini/` (la pagina, `aggiorna.py` e i file di dati).
Il flusso `.github/workflows/aggiorna.yml` (ramo `master`) legge le stazioni del Centro Funzionale della Regione
Toscana, le previsioni ICON-EU ed ECMWF e il radar della Protezione Civile, e pubblica i file di dati aggiornati
accanto alla pagina:

- alle 8:20 UTC il giro completo (misure, previsioni, archivio, radar e avvisi via mail per i posti salvati);
- ogni ora dalle 9:20 alle 18:20 UTC solo misure e previsioni; se quel giorno il giro del mattino è saltato, il primo
  di questi lo fa completo. I giri sono tanti perché GitHub a volte ritarda o salta gli orari programmati.

GitHub però ritarda di ore o salta del tutto molti giri programmati. Per questo il flusso può partire anche
dall'esterno: un servizio come cron-job.org chiama ogni ora, dalle 8:20 alle 18:20 UTC,

    POST https://api.github.com/repos/the-marox/progetti/actions/workflows/aggiorna.yml/dispatches
    Authorization: Bearer <token>
    Accept: application/vnd.github+json
    {"ref": "master", "inputs": {"completo": "false"}}

con un token GitHub «fine-grained» limitato a questo repository e al solo permesso Actions (lettura e scrittura).
Con `completo` a `false` il primo giro della giornata fa comunque quello completo, gli altri solo misure e previsioni.

Nel giro completo partono anche le mail, composte da `aggiorna.py` (comandi `mail` e `newsletter`):

- la mail dei posti del segreto `POSTI`, all'indirizzo `GMAIL_USER`;
- la newsletter: chi ha un account nell'app e mette la spunta «Ricevi la newsletter» riceve la stessa mail sui posti
  salvati nel suo account, quando uno diventa buono o ottimo in un giorno nuovo. Serve il segreto
  `FIREBASE_SERVICE_ACCOUNT` (chiave JSON di un account di servizio del progetto Firebase `radar-porcini`); l'indirizzo
  si prende dall'account e solo se è confermato. Nel registro delle esecuzioni, che è pubblico, solo numeri.

L'esito dell'ultimo aggiornamento è in https://the-marox.github.io/progetti/radar-porcini/esito.json.
Il vecchio flusso `radar-porcini.yml` è disattivato.
