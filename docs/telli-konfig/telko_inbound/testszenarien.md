| Szenario | Erwartung |
|---|---|
| Bekannte Nummer, PLZ korrekt, Termin verschieben | termin_verschoben, alter Slot frei, neuer Termin, neue Erinnerung |
| PLZ dreimal falsch | nicht_verifiziert, keine Termindaten genannt, Weiterleitung |
| Termin morgen früh verschieben | cancel_appointment abgelehnt (Mindestvorlauf), weitergeleitet |
| Störung Internet seit gestern | ticket_erstellt, Ticket im Ticketsystem (Ereignis ticket.erstellt) |
| Termin nur bestätigen | termin_bestaetigt, nichts verändert |
| Unbekannte Nummer, Rechnungsfrage | weitergeleitet |
