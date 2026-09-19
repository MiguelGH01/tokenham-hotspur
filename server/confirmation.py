"""Deterministic confirmation gate: a qualified "yes" is not consent.

The conversation can only be trusted to refuse politely; whether a confirmation
is unqualified is scored, so the check lives in code. Before a BOOK or REGISTER
is delivered, the caller's latest utterance is inspected for the qualifiers that
make it not-yet-consent: a price question, a correction, a request to check an
alternative, or a negation/hold. A blocked submission stays unconfirmed, the
model is told to clarify, and a later plain confirmation passes.

Conservative by design, in the rejecting direction's favour only up to a point:
a plain "yes" in any of the three languages never blocks, and missing audio or
missing context never blocks (a false block burns turns; a false pass burns the
case only when it would also have burned it without the gate).

Adapted from the reference implementation's ``hasUnresolvedQualification``
(pablofd/hackspain, ``src/confirmation.ts``), reduced to the qualifier families
that map onto this flow graph.
"""

import re
import unicodedata

def _normalize(text: str) -> str:
    lowered = unicodedata.normalize("NFKD", (text or "").casefold())
    stripped = "".join(c for c in lowered if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip()

#: Price topic words. Deliberately paired with a question/agreement form below:
#: a bare "insurance" mention must never block a confirmation.
_PRICE_TOPIC = (
    r"\b(?:cost(?:s|ing)?|price[sd]?|pricing|fees?|charges?|pay(?:ment|ing)?|copays?|"
    r"precio[s]?|coste|costo|cuesta|costar|costaria|copago[s]?|tarifa[s]?|pagar|pagare|cobr[ao]r?|"
    r"preu[s]?|costa|costar|copagament[s]?)\b"
)
_PRICE_QUESTION = (
    r"^(?:how much (?:is|would|will|do|does)|what(?:'s| is) the (?:price|cost|fee)|"
    r"cuanto (?:cuesta|es|seria|cobr[ao])|quant (?:es|costa|seria))"
)
_PRICE_BEFORE_AGREEMENT = (
    r"\b(?:before (?:i )?(?:agree|accept|confirm|book)|antes de (?:aceptar|confirmar|reservar|pagar)|"
    r"abans (?:de |d')(?:acceptar|confirmar|reservar|pagar))\b"
)
#: Conjunctions and correction markers: the sentence continues past the "yes".
_CORRECTION = (
    r"\b(?:but|however|except|instead|actually|i meant|i asked for|i said|"
    r"pero|sin embargo|en realidad|en vez de|en lugar de|queria decir|me referia|he pedido|"
    r"en canvi|en lloc de|vaig dir)\b"
)
#: Requests to keep looking or change something.
_ALTERNATIVE_REQUEST = (
    r"\b(?:(?:can|could|would|will)\s+(?:you|we)\s+(?:please\s+)?(?:check|look|find|see|try|change|move|search)|"
    r"check another|look for another|"
    r"(?:puedes|podrias|puede|podria|pots|podrie[sz]?|podeu)\s+(?:mirar|comprobar|buscar|cercar|canviar|cambiar|revisar|provar|probar)|"
    r"busca(?:s|r)? otra|mirar otra)\b"
)
#: Explicit negations and holds. "no, thanks" blocks; "no problem" does not.
_NEGATION = (
    r"^(?:no[,!. ]|not(?:\s+\w+){0,3}\byet\b|wait|stop|hold off|do not book|don'?t book|"
    r"espera|esperi|encara no|de momento no|por ahora no|todavia no|"
    r"no(?:\s+\w+){0,2}\b(?:reserves|reserve|reservis|book|cancel|registrar)\b)"
)
_NEGATION_EXCEPTION = r"^(?:no problem|no worries|no hay problema|no pasa nada|cap problema|sense problema)"
#: An unresolved alternative still on the table ("another doctor", "otra hora").
_ALTERNATIVE_SUBJECT = (
    r"\b(?:another|other|different|otro|otra|altre|altra)\s+"
    r"(?:\w+\s+){0,2}?"
    r"(?:doctor|doctors|time|times|site|sites|location|day|days|appointment|slot|"
    r"medico|medica|hora|horario|sitio|dia|cita|metge|metgessa|lloc)\b"
)


def gate_result(text: str) -> str | None:
    """The reason the utterance is not unqualified consent, or ``None``.

    Order matters for the audit code: price, then correction, then an
    alternative request, then a negation.
    """
    normalized = _normalize(text)
    if not normalized:
        return None
    if re.search(_PRICE_TOPIC, normalized) and (
        re.search(_PRICE_QUESTION, normalized) or re.search(_PRICE_BEFORE_AGREEMENT, normalized)
    ):
        return "price_question"
    if re.search(r"how much", normalized) and re.search(_PRICE_TOPIC, normalized):
        return "price_question"
    if re.search(_CORRECTION, normalized):
        return "correction"
    if re.search(_ALTERNATIVE_REQUEST, normalized) or re.search(_ALTERNATIVE_SUBJECT, normalized):
        return "alternative_request"
    if re.search(_NEGATION_EXCEPTION, normalized):
        return None
    if re.search(_NEGATION, normalized):
        return "negation"
    return None


def has_unresolved_qualification(text: str) -> bool:
    return gate_result(text) is not None
