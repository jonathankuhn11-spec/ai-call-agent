| Szenario | Erwartung |
|---|---|
| Eigentümer EFH, nimmt ersten Termin | termin_gebucht, Termin im System, Erinnerung geplant |
| Mieter | disqualifiziert, kein check_slots |
| Preisfrage, zweimal | Preis-Satz, kein Euro-, Prozent- oder Zahlenwert |
| „Nicht mehr anrufen“ | opt_out, sofortiges Ende, Nummer gesperrt |
| Keine Zeit, „morgen Nachmittag“ | rueckruf_vereinbart, Aufgabe mit Zeitpunkt |
| Ehepartner am Telefon | falsche_person, Lead unverändert |
| Beide Termine passen nicht | zweite Runde, dann Rückruf |
| Rückruf: Termin verschieben | cancel_appointment, neuer Termin gebucht, alte Erinnerung entfallen |
| Mailbox | keine Nachricht, Lead unverändert, Versuch gezählt |
