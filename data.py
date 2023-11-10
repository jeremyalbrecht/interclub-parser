import datetime
from dataclasses import dataclass
from typing import List

import requests
from bs4 import BeautifulSoup as bs


@dataclass
class Team():
    name: str
    code: str
    logo: str


@dataclass
class Row():
    team: Team
    played: int
    won: int
    draw: int
    lost: int
    bonus: int
    penalties: int
    points: int


@dataclass
class Match():
    date: datetime.datetime
    name: str
    teamA: Team
    scoreA: int
    teamB: Team
    scoreB: int
    localisation: str


@dataclass
class Group():
    teams: List[Team]
    rows: List[Row]
    matches: List[Match]


@dataclass
class Competition():
    url: str
    name: str
    groups: List[Group]

    @staticmethod
    def parse(url: str):
        response = requests.get(url)
        soup = bs(response.content, "lxml")
        h2 = soup.find_all("h2")
        for title in h2:
            if "poule unique" in "".join(title.contents).lower():
                table = soup.find("table", class_="classement-poule")
                group = Competition.parse_group(table)
                return Competition(url=url, name="", groups=[group])
            else:
                tables = soup.find_all("table", class_="classement-poule")
                groups = []
                for table in tables:
                    groups.append(Competition.parse_group(table))
                return Competition(url=url, name="", groups=groups)

    @staticmethod
    def parse_group(table) -> Group:
        teams = []
        lines = []
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) == 0:
                continue
            team = Team(name=cells[2].text, code="", logo="")
            row = Row(team=team,
                      played=cells[3].text,
                      won=cells[4].text,
                      draw=cells[5].text,
                      lost=cells[6].text,
                      bonus=cells[8].text,
                      penalties=cells[9].text,
                      points=cells[10].text)
            teams.append(team)
            lines.append(row)
        group = Group(teams=teams, rows=lines, matches=[])
        return group


