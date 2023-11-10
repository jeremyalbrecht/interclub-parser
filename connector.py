from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Tuple, List
import requests
import jsonpickle


from data import Competition, Group


@dataclass
class APIRow():
    club: str
    won: int
    lost: int
    total: int


@dataclass
class APILeaderboard():
    competition_id: str
    rows: List[APIRow]

    @staticmethod
    def fromGroup(competition: Competition, group: Group):
        lb = APILeaderboard(competition_id=competition.url, rows=[])
        for row in group.rows:
            lb.rows.append(APIRow(row.team.name, row.won, row.lost, row.points))
        return lb


@dataclass
class APITeam():
    id: int
    ranking: str
    color: str
    major: bool
    icbad_id: str
    group: str
    leaderboard: APILeaderboard


class Connector(ABC):

    def __init__(self, URL: str, auth: str):
        self.URL = URL
        self.auth = auth

    @abstractmethod
    def getTeams(self) -> List[APITeam]:
        pass

    @abstractmethod
    def pushTeam(self, team: APITeam, competition: Competition):
        pass


class StrapiConnector(Connector):

    def getTeams(self) -> List[APITeam]:
        response = requests.get(url=self.URL + "?populate[0]=teams&populate[1]=teams.leaderboard&populate[2]=teams.leaderboard.rows",
                                headers={"Authorization": "Bearer " + self.auth})
        if response.status_code == 200:
            self.response = response.json()
            return [APITeam(**team) for team in self.response["data"]["attributes"]["teams"]]

    def pushTeam(self, team: APITeam, competiton: Competition):
        updated_response = {}
        updated_response["data"] = {}
        for x in self.response["data"]["attributes"]["teams"]:
            if x["id"] == team.id:
                x["leaderboard"] = APILeaderboard.fromGroup(competiton, competiton.groups[int(team.group)-1])
        updated_response["data"]["teams"] = self.response["data"]["attributes"]["teams"]
        response = requests.put(
            url=self.URL,
            data=jsonpickle.encode(updated_response, unpicklable=False),
            headers={"Authorization": "Bearer " + self.auth, "Content-Type": "application/json"})
        if response.status_code == 200:
            print("success")


