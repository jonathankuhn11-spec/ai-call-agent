"""Datenmodelle der Schnittstellen: tolerant beim Lesen, strikt beim Schreiben.

Eingehende Payloads der Plattform werden mit `extra="ignore"` gelesen: Neue Felder bei telli
dürfen die Integration nicht brechen. Was diese Schicht selbst antwortet oder sendet, ist
exakt nach Kontrakt geformt (Feldnamen, Groß-/Kleinschreibung, Zeitformate).

Quellen (docs.telli.com): Custom Calendar (/available, /book), Contact Lookup Webhook,
Get Call (Call- und Contact-Objekt), Webhooks (Ereignisfeld `event`), Create Contact v2.
Die Hülle des call_ended-Ereignisses ist dort nicht vollständig dokumentiert; gelesen wird
`{"event": ..., "call": {...}, "contact": {...}}` und ersatzweise ein flaches Call-Objekt.
Vor dem Go-live wird das gegen eine echte Zustellung im Webhook-Portal verifiziert
(siehe docs/playbook/02-integrations-checkliste.md).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ERGEBNISSE = ("termin_gebucht", "disqualifiziert", "rueckruf_vereinbart", "opt_out",
              "falsche_person", "kein_interesse", "nicht_erreicht", "sonstiges")

Tolerant = ConfigDict(extra="ignore", populate_by_name=True)
Strikt = ConfigDict(extra="forbid")


def _liste(v):
    return v if v is not None else []


def _dict(v):
    return v if v is not None else {}


def _als_text(v):
    """IDs kommen mal als Zahl, mal als String; intern sind sie immer Strings."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(int(v)) if float(v).is_integer() else str(v)
    return v


def telefon_normalisieren(nummer: Optional[str]) -> str:
    """"+49 251 000001" und "+49251000001" sind dieselbe Nummer; "0251 000001" wird zu +49."""
    if not nummer:
        return ""
    nummer = nummer.replace("(0)", "")
    ziffern = "".join(c for c in nummer if c.isdigit() or c == "+")
    if ziffern.startswith("00"):
        ziffern = "+" + ziffern[2:]
    elif ziffern.startswith("0"):
        ziffern = "+49" + ziffern[1:]
    return ziffern


def iso_utc(dt: datetime) -> str:
    """ISO 8601 in UTC mit Millisekunden und Z, wie es telli für Zeitstempel verlangt."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def iso_lesen(text: str) -> datetime:
    """Liest "2024-01-01T10:00:00.000Z", "...+00:00" und das suffixlose Format der telli-Beispiele (als UTC)."""
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- telli: Kontakt (V2-Form)
class TelliEigenschaft(BaseModel):
    model_config = Tolerant
    key: str
    value: Any = None
    dataType: Optional[str] = None
    label: Optional[str] = None


class TelliKontakt(BaseModel):
    """Kontakt, wie telli ihn an Custom Calendar und Custom Tools mitschickt (camelCase)."""
    model_config = Tolerant
    id: Optional[str] = None
    type: Optional[str] = None
    externalId: Optional[str] = None
    externalUrl: Optional[str] = None
    salutation: Optional[str] = None
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    email: Optional[str] = None
    phoneNumber: Optional[str] = None
    timezoneIana: Optional[str] = None
    autoDialerStatus: Optional[str] = None
    properties: list[TelliEigenschaft] = Field(default_factory=list)

    @field_validator("externalId", "id", "phoneNumber", mode="before")
    @classmethod
    def _text(cls, v):
        return _als_text(v)

    @field_validator("properties", mode="before")
    @classmethod
    def _props(cls, v):
        return _liste(v)

    def eigenschaften(self) -> dict[str, Any]:
        return {p.key: p.value for p in self.properties}


class KontaktReferenz(BaseModel):
    """Gemeinsamer Kopf der Kalender-Anfragen: V2-Kontakt und die weiterhin gesendeten V1-Felder."""
    model_config = Tolerant
    contact: Optional[TelliKontakt] = None
    contact_id: Optional[str] = None
    external_contact_id: Optional[str] = None
    contact_details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("contact_id", "external_contact_id", mode="before")
    @classmethod
    def _text(cls, v):
        return _als_text(v)

    @field_validator("contact_details", mode="before")
    @classmethod
    def _details(cls, v):
        return _dict(v)

    def external_id(self) -> Optional[str]:
        if self.contact and self.contact.externalId:
            return self.contact.externalId
        return self.external_contact_id or None

    def telefon(self) -> str:
        if self.contact and self.contact.phoneNumber:
            return telefon_normalisieren(self.contact.phoneNumber)
        return telefon_normalisieren(self.contact_details.get("phone_number") or self.contact_details.get("phoneNumber"))


# ---------------------------------------------------------------- Custom Calendar (/available, /book)
class VerfuegbarAnfrage(KontaktReferenz):
    pass


class Slot(BaseModel):
    model_config = Strikt
    start_iso: str
    end_iso: str


class VerfuegbarAntwort(BaseModel):
    """Exakt der Kontrakt: nur `available`. Warum eine Liste leer ist, steht im Server-Log, nicht im Body."""
    model_config = Strikt
    available: list[Slot] = Field(default_factory=list)


class BuchenAnfrage(KontaktReferenz):
    start_iso: str


class BuchenAntwort(BaseModel):
    """Immer HTTP 200; Erfolg oder Misserfolg steht im Body (telli-Kontrakt)."""
    model_config = Strikt
    status: Literal["success", "failed"]
    reason: Optional[str] = None


# ---------------------------------------------------------------- Contact Lookup Webhook
class KontaktLookupAnfrage(BaseModel):
    model_config = Tolerant
    event: Literal["contact_lookup"] = "contact_lookup"
    phone_number: str
    to_number: Optional[str] = None


class KontaktLookupKontakt(BaseModel):
    model_config = Strikt
    first_name: str = Field(..., min_length=1, max_length=50)
    last_name: str = Field(..., min_length=1, max_length=50)
    salutation: Optional[str] = None
    email: Optional[str] = None
    external_id: Optional[str] = None
    external_url: Optional[str] = None
    phone_number: Optional[str] = None
    properties: dict[str, Any] = Field(default_factory=dict)


class KontaktLookupAntwort(BaseModel):
    model_config = Strikt
    contact: Optional[KontaktLookupKontakt] = None


# ---------------------------------------------------------------- Custom Tools
class ToolAufruf(BaseModel):
    """Body eines Custom-Tool-Aufrufs. Die drei Systemfelder konfiguriert man in telli als
    System Variables (call.id, contact.externalId, contact.phoneNumber); alle weiteren Felder
    sind LLM-Parameter und werden unverändert an das Tool durchgereicht."""
    model_config = ConfigDict(extra="allow")
    call_id: Optional[str] = None
    external_id: Optional[str] = None
    phone_number: Optional[str] = None

    @field_validator("call_id", "external_id", "phone_number", mode="before")
    @classmethod
    def _text(cls, v):
        return _als_text(v)

    def argumente(self) -> dict[str, Any]:
        return {k: v for k, v in (self.model_extra or {}).items() if v is not None}


# ---------------------------------------------------------------- call_ended (Get-Call-Schema)
class TranskriptEintrag(BaseModel):
    model_config = Tolerant
    role: str
    content: Optional[str] = None
    toolActivity: Optional[str] = None
    toolParameters: Optional[dict[str, Any]] = None


class Outcome(BaseModel):
    model_config = Tolerant
    key: str
    dataType: Optional[str] = None
    value: Any = None
    reason: Optional[str] = None


class GesammeltesFeld(BaseModel):
    model_config = Tolerant
    status: Optional[str] = None
    value: Any = None


class Termin(BaseModel):
    model_config = Tolerant
    id: Optional[str] = None
    starts_at: Optional[str] = None
    timezone: Optional[str] = None
    status: Optional[str] = None
    hosts: list[dict[str, Any]] = Field(default_factory=list)
    reference: Optional[dict[str, Any]] = None

    @field_validator("hosts", mode="before")
    @classmethod
    def _hosts(cls, v):
        return _liste(v)


class FollowUp(BaseModel):
    model_config = Tolerant
    type: Optional[str] = None
    scheduled_at: Optional[str] = None


class TelliAnruf(BaseModel):
    model_config = Tolerant
    call_id: Optional[str] = None
    attempt: Optional[int] = None
    loop_id: Optional[str] = None
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    direction: Optional[str] = None
    external_contact_id: Optional[str] = None
    contact_id: Optional[str] = None
    contact_details: dict[str, Any] = Field(default_factory=dict)
    agent_id: Optional[str] = None
    started_at_iso: Optional[str] = None
    ended_at_iso: Optional[str] = None
    call_length_min: Optional[float] = None
    state: Optional[str] = None
    status: Optional[str] = None
    ended_reason: Optional[str] = None
    follow_up: Optional[FollowUp] = None
    transcript: Optional[str] = None
    transcriptObject: list[TranskriptEintrag] = Field(default_factory=list)
    outcomes: Optional[list[Outcome]] = None
    collected_data: Optional[dict[str, GesammeltesFeld]] = None
    booked_slot_for: Optional[str] = None
    appointments: list[Termin] = Field(default_factory=list)
    recording_url: Optional[str] = None

    @field_validator("call_id", "external_contact_id", "contact_id", "to_number", "from_number", mode="before")
    @classmethod
    def _text(cls, v):
        return _als_text(v)

    @field_validator("outcomes", "transcriptObject", "appointments", mode="before")
    @classmethod
    def _listen(cls, v):
        return _liste(v)

    @field_validator("collected_data", "contact_details", mode="before")
    @classmethod
    def _dicts(cls, v):
        return _dict(v)

    def outcome(self, key: str, standard: Any = None) -> Any:
        for o in self.outcomes or []:
            if o.key == key:
                return o.value
        return standard

    def gesammelt(self, key: str) -> Any:
        feld = (self.collected_data or {}).get(key)
        return feld.value if feld and feld.status == "confirmed" else None

    def gebuchter_termin(self) -> Optional[str]:
        """Zeitpunkt der Buchung laut Plattform; nur `booked` zählt, `pending` oder `unknown` nicht."""
        for t in self.appointments:
            if t.status == "booked" and t.starts_at:
                return t.starts_at
        return None

    def transkript(self) -> list[dict[str, str]]:
        """Ins Format des Agenten (role user/assistant) für die calls-Tabelle; Tool-Aktivität als tool-Zeile."""
        aus = []
        for e in self.transcriptObject:
            if e.toolActivity:
                aus.append({"role": "tool", "content": f"{e.toolActivity} {e.toolParameters or {}}"})
            elif e.content:
                aus.append({"role": "assistant" if e.role == "agent" else "user", "content": e.content})
        if not aus and self.transcript:
            aus.append({"role": "transcript", "content": self.transcript})
        return aus

    def kundenturns(self) -> int:
        return sum(1 for e in self.transcriptObject if e.role == "user" and e.content)


class TelliKontaktV1(BaseModel):
    """Contact-Objekt aus Get Call und (nach dem Schema) dem Webhook: snake_case."""
    model_config = Tolerant
    contact_id: Optional[str] = None
    external_contact_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    contact_details: dict[str, Any] = Field(default_factory=dict)
    status: Optional[str] = None

    @field_validator("contact_id", "external_contact_id", "phone_number", mode="before")
    @classmethod
    def _text(cls, v):
        return _als_text(v)

    @field_validator("contact_details", mode="before")
    @classmethod
    def _details(cls, v):
        return _dict(v)


class CallEndedEreignis(BaseModel):
    model_config = Tolerant
    event: str = "call_ended"
    call: TelliAnruf
    contact: Optional[TelliKontaktV1] = None

    @classmethod
    def lesen(cls, daten: dict[str, Any]) -> "CallEndedEreignis":
        if "call" in daten and isinstance(daten["call"], dict):
            return cls.model_validate(daten)
        # flache Variante: Call-Felder auf oberster Ebene
        return cls(event=daten.get("event", "call_ended"), call=TelliAnruf.model_validate(daten),
                   contact=TelliKontaktV1.model_validate(daten.get("contact") or {}) if daten.get("contact") else None)

    def external_id(self) -> Optional[str]:
        return self.call.external_contact_id or (self.contact.external_contact_id if self.contact else None)

    def telefon(self) -> str:
        if self.contact and self.contact.phone_number:
            return telefon_normalisieren(self.contact.phone_number)
        nummer = self.call.to_number if self.call.direction != "inbound" else self.call.from_number
        return telefon_normalisieren(nummer)


# ---------------------------------------------------------------- Ausgehend: an telli (Create Contact v2, Schedule Call v1)
class TelliKontaktAnlegen(BaseModel):
    model_config = Strikt
    firstName: str = Field(..., min_length=1, max_length=50)
    lastName: str = Field(..., min_length=1, max_length=50)
    phoneNumber: str
    externalId: Optional[str] = None
    externalUrl: Optional[str] = None
    salutation: Optional[str] = None
    timezoneIana: Optional[str] = None
    email: Optional[str] = None
    properties: list[dict[str, Any]] = Field(default_factory=list)


class TelliAnrufPlanen(BaseModel):
    model_config = Strikt
    contact_id: str
    agent_id: str
    max_retry_days: Optional[int] = None
    override_from_number: Optional[str] = None
    schedule: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------- Ausgehend: an Kundensysteme (eigene Ereignisse)
class Ereignis(BaseModel):
    """Was diese Schicht nach außen meldet. Versioniert, damit Empfänger sich auf Felder verlassen können."""
    model_config = Strikt
    id: str
    typ: Literal["anruf.beendet", "termin.gebucht", "aufgabe.erstellt", "lead.gesperrt"]
    version: int = 1
    zeit: str
    lead_id: int
    daten: dict[str, Any] = Field(default_factory=dict)
