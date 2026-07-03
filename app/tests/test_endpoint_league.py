import requests
from helpers import *
from connection import Connection

db = Connection()

# Chars 0-8: Mario, Luigi, DK, Diddy, Peach, Daisy, Yoshi, Baby Mario, Baby Luigi
cSTARTING_NINE = [{'char_id': char_id} for char_id in range(9)]

def setup_league(**league_kwargs):
    """wipe_db, register an admin + a second owner, create an official (public)
    community that both belong to, a Season tag_set, and league settings.
    Returns (admin, owner2, community, tagset, league)."""
    wipe_db()

    admin = User()
    admin.register()
    admin.verify_user()
    assert admin.add_to_group('admin') == True

    owner2 = User()
    owner2.register()
    owner2.verify_user()

    community = Community(admin, official=True, private=False, link=False)
    assert community.success == True

    tagset = TagSet(community.founder, community, [], 'Season')
    assert tagset.create() == True

    league = DraftLeague(admin, community, tagset, **league_kwargs)
    assert league.success == True
    return admin, owner2, community, tagset, league

def build_slots(league_character_ids, overrides=None):
    """9 slots, batting order/fielding pos 0-8 in listed order."""
    slots = list()
    for order, league_character_id in enumerate(league_character_ids):
        slot = {'league_character_id': league_character_id, 'batting_order': order, 'fielding_pos': order}
        if overrides and order in overrides:
            slot.update(overrides[order])
        slots.append(slot)
    return slots

def make_team_with_roster(league, user, name, char_entries):
    """Create a team and add a pool + roster for it (user must be a league admin).
    Returns (team_id, [league_character_ids])."""
    response = league.create_team(user, name)
    assert response.status_code == 200
    team_id = response.json()['id']

    response = league.create_pool(user, char_entries)
    assert response.status_code == 200
    league_character_ids = [c['id'] for c in response.json()['characters']]
    for league_character_id in league_character_ids:
        response = league.roster_add(user, team_id, league_character_id)
        assert response.status_code == 200
    return team_id, league_character_ids

# === Settings ===

def test_league_settings_create_and_get():
    admin, owner2, community, tagset, league = setup_league()

    # Duplicate settings rejected
    duplicate = DraftLeague(admin, community, tagset)
    assert duplicate.success == False

    # Non-admin cannot create a league on a fresh tag_set
    tagset2 = TagSet(community.founder, community, [], 'Season')
    assert tagset2.create() == True
    non_admin_league = DraftLeague(owner2, community, tagset2)
    assert non_admin_league.success == False

    # Bounds validation
    bad_min = DraftLeague(admin, community, tagset2, roster_min=8)
    assert bad_min.success == False
    bad_max = DraftLeague(admin, community, tagset2, roster_min=10, roster_max=9)
    assert bad_max.success == False

    response = league.get_settings()
    assert response.status_code == 200
    data = response.json()
    assert data['roster_min'] == 9
    assert data['roster_max'] == 12
    assert data['require_move_approval'] == False
    assert data['require_trade_approval'] == False

def test_league_settings_update():
    admin, owner2, community, tagset, league = setup_league()

    response = league.update_settings(admin, roster_max=16, require_move_approval=1)
    assert response.status_code == 200
    assert response.json()['roster_max'] == 16
    assert response.json()['require_move_approval'] == True

    # Non-admin cannot update
    response = league.update_settings(owner2, roster_max=20)
    assert response.status_code != 200

    # Invalid bounds
    response = league.update_settings(admin, roster_min=8)
    assert response.status_code != 200
    response = league.update_settings(admin, roster_min=10, roster_max=9)
    assert response.status_code != 200

# === Character pool ===

def test_league_character_pool():
    admin, owner2, community, tagset, league = setup_league()

    # Bulk create with copies and alias resolution
    response = league.create_pool(admin, [{'char_id': 0, 'copies': 2, 'superstar': 1, 'value': 50},
                                          {'name': 'Goomba'}])
    assert response.status_code == 200
    created = response.json()['characters']
    assert len(created) == 3
    marios = [c for c in created if c['char_id'] == 0]
    assert sorted([m['copy_num'] for m in marios]) == [1, 2]
    assert marios[0]['superstar'] == True
    assert marios[0]['value'] == 50
    assert marios[0]['captain_eligible'] == True
    goomba = [c for c in created if c['char_id'] == 40][0]
    assert goomba['copy_num'] == 1
    assert goomba['captain_eligible'] == False

    # A later batch continues the copy_num sequence
    response = league.create_pool(admin, [{'char_id': 0}])
    assert response.status_code == 200
    assert response.json()['characters'][0]['copy_num'] == 3

    # Unknown character rejected
    response = league.create_pool(admin, [{'name': 'NotACharacter'}])
    assert response.status_code != 200
    response = league.create_pool(admin, [{'char_id': 999}])
    assert response.status_code != 200

    # Non-admin cannot create pool characters
    response = league.create_pool(owner2, [{'char_id': 1}])
    assert response.status_code != 200

    # Update an instance
    response = league.update_character(admin, goomba['id'], superstar=1, value=10, batting_hand=1)
    assert response.status_code == 200
    assert response.json()['superstar'] == True
    assert response.json()['value'] == 10
    assert response.json()['batting_hand'] == 1

    # Free agent list matches everything created so far
    response = league.list_characters(free_agents=True)
    assert response.status_code == 200
    assert len(response.json()['characters']) == 4

    # Delete a free agent instance
    response = league.delete_character(admin, goomba['id'])
    assert response.status_code == 200
    result = db.query('SELECT * FROM league_character WHERE id = %s', (str(goomba['id']),))
    assert len(result) == 0

# === Teams ===

def test_league_team_create():
    admin, owner2, community, tagset, league = setup_league()

    response = league.create_team(admin, 'The Fireballs', logo_id=2, stadium_id=4)
    assert response.status_code == 200
    team = response.json()
    assert team['name'] == 'The Fireballs'
    assert team['logo_id'] == 2
    assert team['stadium_id'] == 4

    # One team per user per league
    response = league.create_team(admin, 'Second Team')
    assert response.status_code != 200

    # Name collision within the league
    response = league.create_team(owner2, 'the fireballs')
    assert response.status_code != 200

    # Invalid logo/stadium
    response = league.create_team(owner2, 'Bad Logo Team', logo_id=99)
    assert response.status_code != 200
    response = league.create_team(owner2, 'Bad Stadium Team', stadium_id=99)
    assert response.status_code != 200

    # Admin creates a team on behalf of another member
    response = league.create_team(admin, 'Owner2 Team', username=owner2.username)
    assert response.status_code == 200

    # Non-admin cannot create on behalf of someone else
    third = User()
    third.register()
    community.join_via_request(third)
    response = league.create_team(owner2, 'Sneaky Team', username=third.username)
    assert response.status_code != 200

    response = league.list_teams()
    assert response.status_code == 200
    teams = response.json()['teams']
    assert len(teams) == 2
    assert sorted([t['owner_username'] for t in teams]) == sorted([admin.username, owner2.username])

def test_league_team_captain_and_validity():
    admin, owner2, community, tagset, league = setup_league()

    response = league.create_team(admin, 'Captain Test Team')
    assert response.status_code == 200
    team_id = response.json()['id']

    response = league.create_pool(admin, cSTARTING_NINE + [{'name': 'Goomba'}])
    assert response.status_code == 200
    created = response.json()['characters']
    goomba_id = [c['id'] for c in created if c['char_id'] == 40][0]
    other_ids = [c['id'] for c in created if c['char_id'] != 40]

    # Captain must be ON the team
    response = league.update_team(admin, team_id, captain_league_character_id=goomba_id)
    assert response.status_code != 200

    for league_character_id in created:
        response = league.roster_add(admin, team_id, league_character_id['id'])
        assert response.status_code == 200

    # Any rostered character may captain, even a non-captain-eligible one (Goomba)
    response = league.update_team(admin, team_id, captain_league_character_id=goomba_id)
    assert response.status_code == 200
    assert response.json()['captain_league_character_id'] == goomba_id

    # roster_valid requires a captain AND a complete default lineup
    response = league.get_team(team_id=team_id)
    assert response.status_code == 200
    assert response.json()['roster_valid'] == False #No default lineup yet

    response = league.create_lineup(admin, team_id, 'Main', build_slots(other_ids[:9]))
    assert response.status_code == 200

    response = league.get_team(team_id=team_id)
    assert response.json()['roster_valid'] == True

    # Clearing the captain invalidates the roster again
    response = league.update_team(admin, team_id, captain_league_character_id=None)
    assert response.status_code == 200
    response = league.get_team(team_id=team_id)
    assert response.json()['roster_valid'] == False

# === Roster moves ===

def test_league_roster_moves_no_approval():
    # min == max league: drops below the minimum must still be possible (soft min)
    admin, owner2, community, tagset, league = setup_league(roster_min=10, roster_max=10)

    response = league.create_team(admin, 'Movers')
    team_id = response.json()['id']
    response = league.create_pool(admin, [{'char_id': char_id} for char_id in range(11)])
    league_character_ids = [c['id'] for c in response.json()['characters']]

    # Adds execute immediately when approvals are off
    for league_character_id in league_character_ids[:10]:
        response = league.roster_add(admin, team_id, league_character_id)
        assert response.status_code == 200
        assert response.json()['status'] == 'Executed'

    # roster_max hard block
    response = league.roster_add(admin, team_id, league_character_ids[10])
    assert response.status_code != 200

    # Cannot add a non-free-agent
    response = league.create_team(admin, 'Other Team', username=owner2.username)
    other_team_id = response.json()['id']
    response = league.roster_add(owner2, other_team_id, league_character_ids[0])
    assert response.status_code != 200

    # Owner cannot manage a team they don't own
    response = league.roster_drop(owner2, team_id, league_character_ids[0])
    assert response.status_code != 200

    # Drop below roster_min succeeds with a warning (soft minimum)
    response = league.roster_drop(admin, team_id, league_character_ids[0])
    assert response.status_code == 200
    assert response.json()['status'] == 'Executed'
    assert len(response.json()['warnings']) > 0
    result = db.query('SELECT * FROM league_character WHERE id = %s', (str(league_character_ids[0]),))
    assert result[0]['league_team_id'] == None

def test_league_roster_moves_with_approval():
    admin, owner2, community, tagset, league = setup_league(require_move_approval=True)

    response = league.create_team(admin, 'Owner2 Squad', username=owner2.username)
    team_id = response.json()['id']
    response = league.create_pool(admin, [{'char_id': char_id} for char_id in range(3)])
    league_character_ids = [c['id'] for c in response.json()['characters']]

    # Owner-submitted add sits Pending
    response = league.roster_add(owner2, team_id, league_character_ids[0])
    assert response.status_code == 200
    move_id = response.json()['id']
    assert response.json()['status'] == 'Pending'

    # Same instance cannot get a second pending move
    response = league.roster_add(owner2, team_id, league_character_ids[0])
    assert response.status_code != 200

    # Only admins respond to moves
    response = league.respond_move(owner2, move_id, accept=True)
    assert response.status_code != 200

    response = league.respond_move(admin, move_id, accept=True)
    assert response.status_code == 200
    assert response.json()['status'] == 'Executed'
    result = db.query('SELECT * FROM league_character WHERE id = %s', (str(league_character_ids[0]),))
    assert result[0]['league_team_id'] == team_id

    # Rejection leaves the instance untouched
    response = league.roster_add(owner2, team_id, league_character_ids[1])
    move_id = response.json()['id']
    response = league.respond_move(admin, move_id, accept=False)
    assert response.status_code == 200
    assert response.json()['status'] == 'Rejected'
    result = db.query('SELECT * FROM league_character WHERE id = %s', (str(league_character_ids[1]),))
    assert result[0]['league_team_id'] == None

    # Owner cancels their own pending move
    response = league.roster_add(owner2, team_id, league_character_ids[2])
    move_id = response.json()['id']
    response = league.cancel_move(owner2, move_id)
    assert response.status_code == 200
    assert response.json()['status'] == 'Cancelled'

    # Admin-submitted moves skip approval entirely
    response = league.roster_add(admin, team_id, league_character_ids[1])
    assert response.status_code == 200
    assert response.json()['status'] == 'Executed'

# === Lineups ===

def test_league_lineups():
    admin, owner2, community, tagset, league = setup_league(roster_max=16)

    response = league.create_team(admin, 'Lineup Team')
    team_id = response.json()['id']
    response = league.create_pool(admin, [{'char_id': char_id} for char_id in range(10)])
    created = response.json()['characters']
    league_character_ids = [c['id'] for c in created]
    for league_character_id in league_character_ids:
        league.roster_add(admin, team_id, league_character_id)

    # Invalid shapes
    response = league.create_lineup(admin, team_id, 'Short', build_slots(league_character_ids[:8]))
    assert response.status_code != 200
    dup_batting = build_slots(league_character_ids[:9])
    dup_batting[1]['batting_order'] = 0
    response = league.create_lineup(admin, team_id, 'DupBatting', dup_batting)
    assert response.status_code != 200
    dup_pos = build_slots(league_character_ids[:9])
    dup_pos[1]['fielding_pos'] = 0
    response = league.create_lineup(admin, team_id, 'DupPos', dup_pos)
    assert response.status_code != 200
    dup_char = build_slots(league_character_ids[:8] + [league_character_ids[0]])
    response = league.create_lineup(admin, team_id, 'DupChar', dup_char)
    assert response.status_code != 200

    # Char 0 (Mario) bats right (0) naturally; override to lefty in slot 0 only
    slots = build_slots(league_character_ids[:9], overrides={0: {'batting_hand': 1, 'superstar': 1}})
    response = league.create_lineup(admin, team_id, 'Main', slots)
    assert response.status_code == 200
    lineup = response.json()
    lineup_id = lineup['id']
    assert lineup['is_default'] == True #First lineup auto-defaults
    assert lineup['slots'][0]['batting_hand'] == 1 #Override wins
    assert lineup['slots'][0]['superstar'] == True
    assert lineup['slots'][1]['batting_hand'] == created[1]['batting_hand'] #Inherited

    # Second lineup, then switch default
    response = league.create_lineup(admin, team_id, 'Alt', build_slots(league_character_ids[1:10]))
    assert response.status_code == 200
    alt_lineup_id = response.json()['id']
    assert response.json()['is_default'] == False

    response = league.set_default_lineup(admin, alt_lineup_id)
    assert response.status_code == 200
    result = db.query('SELECT * FROM league_lineup WHERE id = %s', (str(lineup_id),))
    assert result[0]['is_default'] == False

    # Full slot replace via update
    response = league.update_lineup(admin, lineup_id, slots=build_slots(league_character_ids[1:10]))
    assert response.status_code == 200

    # Non-owner cannot touch lineups
    response = league.delete_lineup(owner2, lineup_id)
    assert response.status_code != 200

    response = league.delete_lineup(admin, lineup_id)
    assert response.status_code == 200
    result = db.query('SELECT * FROM league_lineup_slot WHERE league_lineup_id = %s', (str(lineup_id),))
    assert len(result) == 0

# === Trades ===

def test_league_trades_no_approval():
    admin, owner2, community, tagset, league = setup_league(roster_max=16)

    team_a, roster_a = make_team_with_roster(league, admin, 'Team A', cSTARTING_NINE)
    response = league.create_team(admin, 'Team B', username=owner2.username)
    team_b = response.json()['id']
    response = league.create_pool(admin, [{'char_id': char_id} for char_id in range(9, 18)])
    roster_b = [c['id'] for c in response.json()['characters']]
    for league_character_id in roster_b:
        league.roster_add(owner2, team_b, league_character_id)

    # Team A default lineup includes the character about to be traded away
    response = league.create_lineup(admin, team_a, 'Main', build_slots(roster_a))
    assert response.status_code == 200
    lineup_a = response.json()['id']

    # Ownership validated at proposal
    response = league.propose_trade(admin, team_a, team_b, [roster_b[0]], [roster_a[0]])
    assert response.status_code != 200

    # Owner2 cannot propose on behalf of team A
    response = league.propose_trade(owner2, team_a, team_b, [roster_a[0]], [roster_b[0]])
    assert response.status_code != 200

    response = league.propose_trade(admin, team_a, team_b, [roster_a[0]], [roster_b[0]])
    assert response.status_code == 200
    trade_id = response.json()['id']
    assert response.json()['status'] == 'Proposed'

    # A second trade offering the same character (to be auto-cancelled later)
    response = league.propose_trade(admin, team_a, team_b, [roster_a[0]], [roster_b[1]])
    conflicting_trade_id = response.json()['id']

    # Only the receiving owner can respond
    response = league.respond_trade(admin, trade_id, accept=True)
    assert response.status_code != 200

    response = league.respond_trade(owner2, trade_id, accept=True)
    assert response.status_code == 200
    assert response.json()['status'] == 'Executed'

    # Instances swapped
    result = db.query('SELECT * FROM league_character WHERE id = %s', (str(roster_a[0]),))
    assert result[0]['league_team_id'] == team_b
    result = db.query('SELECT * FROM league_character WHERE id = %s', (str(roster_b[0]),))
    assert result[0]['league_team_id'] == team_a

    # Traded character's lineup slot deleted
    result = db.query('SELECT * FROM league_lineup_slot WHERE league_lineup_id = %s', (str(lineup_a),))
    assert len(result) == 8

    # Conflicting open trade auto-cancelled
    result = db.query('SELECT * FROM league_trade WHERE id = %s', (str(conflicting_trade_id),))
    assert result[0]['status'] == 'Cancelled'

    # Reject flow
    response = league.propose_trade(admin, team_a, team_b, [roster_a[1]], [roster_b[1]])
    trade_id = response.json()['id']
    response = league.respond_trade(owner2, trade_id, accept=False)
    assert response.status_code == 200
    assert response.json()['status'] == 'Rejected'

    # Cancel flow (proposer side)
    response = league.propose_trade(admin, team_a, team_b, [roster_a[1]], [roster_b[1]])
    trade_id = response.json()['id']
    response = league.cancel_trade(admin, trade_id)
    assert response.status_code == 200
    assert response.json()['status'] == 'Cancelled'

def test_league_trades_with_approval():
    admin, owner2, community, tagset, league = setup_league(roster_max=16, require_trade_approval=True)

    team_a, roster_a = make_team_with_roster(league, admin, 'Team A', cSTARTING_NINE)
    response = league.create_team(admin, 'Team B', username=owner2.username)
    team_b = response.json()['id']
    response = league.create_pool(admin, [{'char_id': char_id} for char_id in range(9, 18)])
    roster_b = [c['id'] for c in response.json()['characters']]
    for league_character_id in roster_b:
        league.roster_add(owner2, team_b, league_character_id)

    # Accepted trade waits for admin approval
    response = league.propose_trade(admin, team_a, team_b, [roster_a[0]], [roster_b[0]])
    trade_id = response.json()['id']
    response = league.respond_trade(owner2, trade_id, accept=True)
    assert response.status_code == 200
    assert response.json()['status'] == 'Accepted'
    result = db.query('SELECT * FROM league_character WHERE id = %s', (str(roster_a[0]),))
    assert result[0]['league_team_id'] == team_a #Not moved yet

    # Non-admin cannot execute
    response = league.execute_trade(owner2, trade_id)
    assert response.status_code != 200

    response = league.execute_trade(admin, trade_id)
    assert response.status_code == 200
    assert response.json()['status'] == 'Executed'
    result = db.query('SELECT * FROM league_character WHERE id = %s', (str(roster_a[0]),))
    assert result[0]['league_team_id'] == team_b

    # Veto flow
    response = league.propose_trade(admin, team_a, team_b, [roster_a[1]], [roster_b[1]])
    trade_id = response.json()['id']
    response = league.veto_trade(admin, trade_id)
    assert response.status_code == 200
    assert response.json()['status'] == 'Vetoed'

    # Stale ownership: drop an offered character, then try to execute
    response = league.propose_trade(admin, team_a, team_b, [roster_a[2]], [roster_b[2]])
    trade_id = response.json()['id']
    response = league.roster_drop(admin, team_a, roster_a[2])
    assert response.status_code == 200
    # The drop auto-cancels the open trade referencing the dropped character
    result = db.query('SELECT * FROM league_trade WHERE id = %s', (str(trade_id),))
    assert result[0]['status'] == 'Cancelled'

def test_league_trade_admin_force_execute():
    admin, owner2, community, tagset, league = setup_league(roster_max=16)

    team_a, roster_a = make_team_with_roster(league, admin, 'Team A', cSTARTING_NINE)
    response = league.create_team(admin, 'Team B', username=owner2.username)
    team_b = response.json()['id']
    response = league.create_pool(admin, [{'char_id': 9}])
    roster_b = [c['id'] for c in response.json()['characters']]
    league.roster_add(owner2, team_b, roster_b[0])

    # Admin proposes with execute flag: runs in one call, no acceptance needed
    response = league.propose_trade(admin, team_a, team_b, [roster_a[0]], [roster_b[0]], execute=True)
    assert response.status_code == 200
    assert response.json()['status'] == 'Executed'
    result = db.query('SELECT * FROM league_character WHERE id = %s', (str(roster_b[0]),))
    assert result[0]['league_team_id'] == team_a

    # Non-admin cannot use the execute flag
    response = league.propose_trade(owner2, team_b, team_a, [roster_a[0]], [roster_a[1]], execute=True)
    assert response.status_code != 200

# === Game load ===

def test_league_game_load():
    admin, owner2, community, tagset, league = setup_league(roster_max=16)

    team_a, roster_a = make_team_with_roster(league, admin, 'Team A', cSTARTING_NINE)
    response = league.create_team(admin, 'Team B', username=owner2.username)
    team_b = response.json()['id']
    response = league.create_pool(admin, [{'char_id': char_id, 'superstar': 1} for char_id in range(9, 18)])
    roster_b = [c['id'] for c in response.json()['characters']]
    for league_character_id in roster_b:
        league.roster_add(owner2, team_b, league_character_id)

    # No team for a user
    third = User()
    third.register()
    community.join_via_request(third)
    response = league.game_load([admin.username, third.username])
    assert response.status_code != 200

    # No default lineup yet
    response = league.game_load([admin.username, owner2.username])
    assert response.status_code != 200

    response = league.create_lineup(admin, team_a, 'Main', build_slots(roster_a, overrides={3: {'batting_hand': 1}}))
    assert response.status_code == 200
    response = league.create_lineup(owner2, team_b, 'Main', build_slots(roster_b))
    assert response.status_code == 200

    # Captain still unset
    response = league.game_load([admin.username, owner2.username])
    assert response.status_code != 200

    assert league.update_team(admin, team_a, captain_league_character_id=roster_a[3]).status_code == 200
    assert league.update_team(owner2, team_b, captain_league_character_id=roster_b[0]).status_code == 200

    response = league.game_load([admin.username, owner2.username])
    assert response.status_code == 200
    data = response.json()
    assert data['tag_set_id'] == league.tag_set_id
    assert len(data['teams']) == 2

    team_a_data = [t for t in data['teams'] if t['username'] == admin.username][0]
    assert team_a_data['team_id'] == team_a
    assert team_a_data['captain_league_character_id'] == roster_a[3]
    assert team_a_data['captain_roster_loc'] == 3
    assert len(team_a_data['lineup']) == 9
    assert [slot['batting_order'] for slot in team_a_data['lineup']] == list(range(9))
    assert team_a_data['lineup'][3]['captain'] == True
    assert team_a_data['lineup'][3]['batting_hand'] == 1 #Slot override resolved
    assert team_a_data['lineup'][0]['captain'] == False

    team_b_data = [t for t in data['teams'] if t['username'] == owner2.username][0]
    assert all([slot['superstar'] == True for slot in team_b_data['lineup']]) #Inherited from instances

# === TagSet deletion guard ===

def test_tag_set_delete_blocked_for_league():
    admin, owner2, community, tagset, league = setup_league()

    response = requests.post(f"{BASE_URL}/tag_set/delete", json={'name': tagset.name, 'rio_key': admin.rk})
    assert response.status_code != 200

    # A non-league tag_set still deletes fine
    tagset2 = TagSet(community.founder, community, [], 'Season')
    assert tagset2.create() == True
    response = requests.post(f"{BASE_URL}/tag_set/delete", json={'name': tagset2.name, 'rio_key': admin.rk})
    assert response.status_code == 200
