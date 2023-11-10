import os

from bs4 import BeautifulSoup as bs
import requests
from dotenv import load_dotenv

from data import Competition
from connector import StrapiConnector

load_dotenv()

BASE_URL = os.getenv("BASE_URL")
API_URL = os.getenv("API_URL")
TOKEN = os.getenv("API_TOKEN")


def extract_league(region: str) -> Competition:
    response = requests.get(BASE_URL)
    soup = bs(response.content, "lxml")
    links = soup.find_all("a", class_="link")
    for l in links:
        if region.lower() in "".join(l.contents).lower():
            competition = Competition.parse(l.attrs["href"])
            return competition


if __name__ == '__main__':
    connector = StrapiConnector(API_URL, TOKEN)
    teams = connector.getTeams()
    for team in teams:
        if team.major:
            competition = extract_league(team.icbad_id)
            connector.pushTeam(team, competition)

