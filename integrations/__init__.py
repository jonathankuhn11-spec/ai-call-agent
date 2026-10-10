"""
Integrationsschicht: verbindet den Call-Agenten mit dem Tech-Stack eines Kunden.

Der Agent in agent.py und guided.py ist die eine Hälfte eines Deployments. Die andere Hälfte
ist alles, was um das Gespräch herum passiert: Leads aus dem CRM in den Dialer schieben,
dem Agenten während des Gesprächs Backend-Funktionen geben (Kunde nachschlagen, Termine
prüfen, buchen), das Gesprächsergebnis ins CRM zurückschreiben, Aufgaben für Rückrufe anlegen,
nachgelagerte Systeme benachrichtigen. Dieses Paket baut genau diese Hälfte, und zwar entlang
der dokumentierten Schnittstellen der Voice-Plattform telli (docs.telli.com):

  Richtung              Schnittstelle                         Modul
  CRM -> Plattform      Create Contact v2, Schedule Call v1   telli.py
  Plattform -> Backend  Custom Tools (HTTP-Function-Tools)    server.py (/tools/{name})
  Plattform -> Backend  Custom Calendar (/available, /book)   server.py, kalender.py
  Plattform -> Backend  Contact Lookup Webhook                server.py
  Plattform -> Backend  call_ended-Webhook (Svix-signiert)    server.py, nachbereitung.py
  Backend -> Kunde      signierte Webhooks mit Outbox         outbox.py

Der Grundsatz des Agenten gilt auch hier: Der Code entscheidet. Jede Buchung, jede
Statusänderung wird gegen den Zustand des CRM geprüft, nicht gegen die Behauptung eines
Modells oder einer Plattform. Alles läuft lokal gegen das Mock-CRM in DuckDB; der
HubSpot-Adapter ist gegen die dokumentierte API-Form getestet, nicht gegen einen Live-Account.
"""

__version__ = "1.0.0"
