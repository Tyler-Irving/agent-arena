"""System prompts, message templates and fact lines for the Mafia roles.

All prompt text lives here so lineups are compared on the same wording.
Players see code-written fact lines and each other's speeches, never the
narrator's text, so the narrator can't leak or distort anything.
"""

from arena.plugins.mafia.game import GameState, Player, Role

MAX_SPEECH_CHARS = 600  # longer speeches are cut before anyone sees them

PLAYER_SYSTEM = f"""\
You are a player in a game of Mafia, a social deduction game played in text.

Rules:
- Every player is secretly town (villagers and one detective) or mafia. Mafia know each other; \
town players know only their own role.
- The game alternates night and day, starting with night 1.
- Each night the mafia choose one town player to kill, and the detective privately learns \
whether one player is mafia.
- Each day every living player speaks once, in turn, then everyone votes to eliminate one \
player. Votes are secret until everyone has voted, then all are revealed together. The \
player with the most votes is eliminated; a tie is settled at random; if no one casts a \
valid vote, no one is eliminated.
- When a player dies or is eliminated, their role is revealed.
- Town wins when every mafia player is gone. Mafia wins once mafia are at least as many as town.

Only your speeches are shared with the other players, cut to {MAX_SPEECH_CHARS} characters. \
Your reasoning and your night choices stay private. Play to win for your side: town by \
finding the mafia, mafia by not being found."""

NARRATOR_SYSTEM = """\
You are the narrator of a game of Mafia. Turn the facts you are given into a short, \
atmospheric announcement of two or three sentences. State every fact as given, add no new \
facts, and never guess anyone's role."""

# --- fact lines: the only shared record of what happened ------------------------


def _role_phrase(role: Role) -> str:
    return {"mafia": "mafia", "detective": "the detective", "villager": "a villager"}[role]


def death_fact(night: int, player: Player) -> str:
    return f"Night {night}: {player.name} was killed. {player.name} was {_role_phrase(player.role)}."


def speech_line(day: int, name: str, text: str) -> str:
    return f"Day {day}, {name} said: {text}"


def votes_fact(day: int, votes: dict[str, str | None]) -> str:
    cast = ", ".join(f"{voter} -> {target or 'none'}" for voter, target in votes.items())
    return f"Day {day} votes: {cast}."


def elimination_fact(day: int, player: Player | None) -> str:
    if player is None:
        return f"Day {day}: no one was eliminated."
    return f"Day {day}: {player.name} was eliminated. {player.name} was {_role_phrase(player.role)}."


def check_result(night: int, name: str | None, is_mafia: bool) -> str:
    if name is None:
        return f"Night {night} check: wasted (your reply had no valid CHECK line)."
    return f"Night {night} check: {name} is {'mafia' if is_mafia else 'not mafia'}."


# --- what one player sees -----------------------------------------------------------


def private_info(state: GameState, player: Player, checks: list[str]) -> str:
    lines = [f"You are {player.name}."]
    if player.role == "villager":
        lines.append("You are a villager (town).")
    elif player.role == "detective":
        lines.append("You are the detective (town).")
        lines += (
            ["Your checks so far:", *(f"- {c}" for c in checks)]
            if checks
            else ["You have not checked anyone yet."]
        )
    else:
        lines.append("You are mafia.")
        partners = [p for p in state.players if p.role == "mafia" and p is not player]
        if not partners:
            lines.append("You are the only mafia player.")
        for p in partners:
            lines.append(f"Your mafia partner is {p.name}{'' if p.alive else ' (dead)'}.")
    return "\n".join(lines)


def seating(state: GameState) -> str:
    seats = [p.name if p.alive else f"{p.name} (dead)" for p in state.players]
    return "Players, in seat order: " + ", ".join(seats)


def player_prompt(state: GameState, player: Player, checks: list[str], public: list[str], task: str) -> str:
    log = "\n".join(public) if public else "(nothing yet)"
    return (
        f"{private_info(state, player, checks)}\n\n{seating(state)}\n\n"
        f"What has happened so far:\n{log}\n\n{task}"
    )


# --- tasks ----------------------------------------------------------------------------


def kill_task(night: int, targets: list[str], proposal: tuple[str, str] | None, partner_decides: bool) -> str:
    task = f"It is night {night}. As mafia, choose a town player to kill."
    if proposal is not None:
        who, text = proposal
        task += f"\n\nYour partner {who} proposed:\n{text}\n\nYou make the final choice."
    elif partner_decides:
        task += " Your partner will see your proposal and make the final choice."
    return f"{task}\n\nEnd your reply with one line: TARGET: <name>, choosing from {', '.join(targets)}."


def check_task(night: int, targets: list[str]) -> str:
    return (
        f"It is night {night}. Choose one player to investigate; you will learn privately whether "
        f"they are mafia.\n\nEnd your reply with one line: CHECK: <name>, choosing from {', '.join(targets)}."
    )


def speech_task(day: int) -> str:
    return (
        f"It is day {day} and your turn to speak to the town. Say what you think and why, in at "
        "most 80 words. Everything you write here is shared."
    )


def vote_task(day: int, targets: list[str]) -> str:
    return (
        f"It is day {day} and everyone has spoken. Vote to eliminate one player. Your vote stays "
        "secret until everyone has voted.\n\n"
        f"End your reply with one line: VOTE: <name>, choosing from {', '.join(targets)}, "
        "or VOTE: none to abstain."
    )


def narrate_task(facts: list[str]) -> str:
    return "Facts:\n" + "\n".join(f"- {f}" for f in facts)
