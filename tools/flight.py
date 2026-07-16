import httpx
from config import settings

SCHEMA = {
    "name": "flight_status",
    "description": (
        "Get real-time status of a flight by IATA flight number (e.g. LH441, AA100). "
        "Returns departure/arrival times and current status."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "flight_number": {
                "type": "string",
                "description": "IATA flight number, e.g. LH441",
            }
        },
        "required": ["flight_number"],
    },
}

_BASE = "http://api.aviationstack.com/v1"


async def flight_status(flight_number: str) -> str:
    params = {
        "access_key": settings.aviationstack_api_key,
        "flight_iata": flight_number.upper(),
        "limit": 1,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{_BASE}/flights", params=params)
        resp.raise_for_status()

    data = resp.json()
    flights = data.get("data", [])
    if not flights:
        return f"No data found for flight {flight_number.upper()}."

    f = flights[0]
    status = f.get("flight_status", "unknown").capitalize()
    dep = f.get("departure", {})
    arr = f.get("arrival", {})

    dep_airport = dep.get("iata", "?")
    arr_airport = arr.get("iata", "?")
    dep_sched = (dep.get("scheduled") or "?")[:16].replace("T", " ")
    arr_sched = (arr.get("scheduled") or "?")[:16].replace("T", " ")
    dep_delay = dep.get("delay")

    delay_str = f" (+{dep_delay}m)" if dep_delay else ""
    return (
        f"{flight_number.upper()}: {status}. "
        f"{dep_airport} {dep_sched}{delay_str} → {arr_airport} {arr_sched}"
    )
