import httpx

async def get_weather(city: str) -> str:
    """Fetch current weather from wttr.in — free, no API key needed."""
    url = f"https://wttr.in/{city.replace(' ', '+')}?format=j1"
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "curl/7.0"})
        resp.raise_for_status()
    data = resp.json()
    cur = data["current_condition"][0]
    area = data["nearest_area"][0]
    city_name = area["areaName"][0]["value"]
    country = area["country"][0]["value"]
    temp_c = cur["temp_C"]
    feels_c = cur["FeelsLikeC"]
    desc = cur["weatherDesc"][0]["value"]
    humidity = cur["humidity"]
    wind_kmph = cur["windspeedKmph"]
    return (
        f"{city_name}, {country}: {desc}, {temp_c}°C (feels {feels_c}°C). "
        f"Humidity {humidity}%, wind {wind_kmph} km/h."
    )
