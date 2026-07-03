from flask import request, jsonify, abort
from flask import current_app as app
from flask_jwt_extended import jwt_required
from ..models import db, RioUser, CommunityUser, Community, TagSet, Character, \
    LeagueSettings, LeagueTeam, LeagueCharacter, LeagueLineup, LeagueLineupSlot, \
    LeagueTrade, LeagueTradeItem, LeagueMove
from ..consts import *
from ..util import *
from ..user_util import *
from app.views.user_groups import *
import time

# === Helpers ===

def get_league(in_tag_set_id, require_settings=True):
    tag_set = TagSet.query.filter_by(id=in_tag_set_id).first()
    if tag_set == None:
        abort(409, description=f'Could not find TagSet with id={in_tag_set_id}')
    settings = LeagueSettings.query.filter_by(tag_set_id=tag_set.id).first()
    if require_settings and settings == None:
        abort(409, description=f'TagSet with id={in_tag_set_id} is not a draft league (no league settings)')
    comm = Community.query.filter_by(id=tag_set.community_id).first()
    if comm == None:
        abort(409, description=f'Could not find community for TagSet with id={in_tag_set_id}')
    return tag_set, settings, comm

def get_acting_user(in_request, comm):
    # Returns (user, comm_user, is_league_admin). Rio admins are league admins even without membership
    user = get_user(in_request)
    if user == None:
        abort(409, description='No user logged in or associated with key.')
    comm_user = CommunityUser.query.filter_by(user_id=user.id, community_id=comm.id).first()
    is_league_admin = (comm_user != None and comm_user.admin) or is_user_in_groups(user.id, ['Admin', 'TrustedUser'])
    if not is_league_admin:
        if comm_user == None or not comm_user.active or comm_user.banned:
            abort(409, description='User is not an active member of this community.')
    return user, comm_user, is_league_admin

def check_read_access(in_request, comm):
    if comm.private == False:
        return
    user = get_user(in_request)
    if user == None:
        abort(409, description='Must be logged in to view a private community league.')
    if is_user_in_groups(user.id, ['Admin', 'TrustedUser']):
        return
    comm_user = CommunityUser.query.filter_by(user_id=user.id, community_id=comm.id).first()
    if comm_user == None:
        abort(409, description='Must be a member of the private community to view this league.')

def get_team(in_team_id):
    team = LeagueTeam.query.filter_by(id=in_team_id).first()
    if team == None:
        abort(409, description=f'Could not find team with id={in_team_id}')
    return team

def user_owns_team(comm_user, team):
    return comm_user != None and team.community_user_id == comm_user.id

def get_roster_count(team_id):
    return LeagueCharacter.query.filter_by(league_team_id=team_id).count()

def get_team_owner_username(team):
    comm_user = CommunityUser.query.filter_by(id=team.community_user_id).first()
    rio_user = RioUser.query.filter_by(id=comm_user.user_id).first()
    return rio_user.username

def get_roster_valid(team, settings):
    # Soft validity: size within [min, max], a captain declared, and a complete default lineup
    roster_count = get_roster_count(team.id)
    if settings.roster_min != None and roster_count < settings.roster_min:
        return False
    if settings.roster_max != None and roster_count > settings.roster_max:
        return False
    if team.captain_league_character_id == None:
        return False
    default_lineup = LeagueLineup.query.filter_by(league_team_id=team.id, is_default=True).first()
    if default_lineup == None or len(default_lineup.slots) != 9:
        return False
    return True

def remove_character_team_artifacts(league_character, warnings):
    # Delete lineup slots referencing a departing instance and clear team captain if needed
    slots = LeagueLineupSlot.query.filter_by(league_character_id=league_character.id).all()
    for slot in slots:
        lineup = slot.league_lineup
        warnings.append(f'Lineup "{lineup.name}" (id={lineup.id}) lost a character and is now incomplete')
        db.session.delete(slot)
    team = LeagueTeam.query.filter_by(id=league_character.league_team_id).first()
    if team != None and team.captain_league_character_id == league_character.id:
        team.captain_league_character_id = None
        db.session.add(team)
        warnings.append(f'Team "{team.name}" no longer has a captain')

def cancel_conflicting_open_items(league_character_ids, warnings, exclude_trade_id=None, exclude_move_id=None):
    # Auto-cancel other open trades/moves referencing instances that just changed hands
    open_trades = LeagueTrade.query.join(LeagueTradeItem).filter(
        LeagueTrade.status.in_(['Proposed', 'Accepted']),
        LeagueTradeItem.league_character_id.in_(league_character_ids)).all()
    for trade in open_trades:
        if trade.id == exclude_trade_id:
            continue
        trade.status = 'Cancelled'
        trade.date_resolved = int( time.time() )
        db.session.add(trade)
        warnings.append(f'Open trade with id={trade.id} was auto-cancelled')
    open_moves = LeagueMove.query.filter(
        LeagueMove.status == 'Pending',
        LeagueMove.league_character_id.in_(league_character_ids)).all()
    for move in open_moves:
        if move.id == exclude_move_id:
            continue
        move.status = 'Cancelled'
        move.date_resolved = int( time.time() )
        db.session.add(move)
        warnings.append(f'Pending move with id={move.id} was auto-cancelled')

def execute_move(move, settings, resolver_comm_user_id):
    # Re-validates then executes an add/drop. Returns warnings list. Aborts leave state unchanged
    warnings = list()
    league_character = LeagueCharacter.query.filter_by(id=move.league_character_id).first()
    team = LeagueTeam.query.filter_by(id=move.league_team_id).first()
    if move.move_type == 'Add':
        if league_character.league_team_id != None:
            abort(409, description='Character instance is no longer a free agent')
        if settings.roster_max != None and get_roster_count(team.id) + 1 > settings.roster_max:
            abort(410, description='Team roster is full (roster_max reached)')
        league_character.league_team_id = team.id
    else: #Drop
        if league_character.league_team_id != team.id:
            abort(409, description='Character instance is not on this team')
        remove_character_team_artifacts(league_character, warnings)
        league_character.league_team_id = None
        if settings.roster_min != None and get_roster_count(team.id) < settings.roster_min:
            warnings.append(f'Team "{team.name}" roster is below roster_min ({settings.roster_min})')
    cancel_conflicting_open_items([league_character.id], warnings, exclude_move_id=move.id)
    move.status = 'Executed'
    move.date_resolved = int( time.time() )
    move.resolved_by_comm_user_id = resolver_comm_user_id
    db.session.add(move)
    db.session.add(league_character)
    db.session.commit()
    return warnings

def execute_trade(trade, settings, resolver_comm_user_id):
    # Re-validates then executes a trade. Returns warnings list. Aborts leave state unchanged
    warnings = list()
    from_team = LeagueTeam.query.filter_by(id=trade.from_team_id).first()
    to_team = LeagueTeam.query.filter_by(id=trade.to_team_id).first()
    from_items = [item for item in trade.items if item.from_team_id == trade.from_team_id]
    to_items = [item for item in trade.items if item.from_team_id == trade.to_team_id]

    # Instances may have moved since the trade was proposed
    for item in trade.items:
        if item.league_character.league_team_id != item.from_team_id:
            abort(409, description=f'Character instance with id={item.league_character_id} is no longer on the offering team')
    if settings.roster_max != None:
        if get_roster_count(from_team.id) - len(from_items) + len(to_items) > settings.roster_max:
            abort(410, description=f'Trade would put team "{from_team.name}" over roster_max')
        if get_roster_count(to_team.id) - len(to_items) + len(from_items) > settings.roster_max:
            abort(410, description=f'Trade would put team "{to_team.name}" over roster_max')

    moved_league_character_ids = list()
    for item in trade.items:
        league_character = item.league_character
        remove_character_team_artifacts(league_character, warnings)
        league_character.league_team_id = trade.to_team_id if (item.from_team_id == trade.from_team_id) else trade.from_team_id
        db.session.add(league_character)
        moved_league_character_ids.append(league_character.id)

    if settings.roster_min != None:
        for team in [from_team, to_team]:
            if get_roster_count(team.id) < settings.roster_min:
                warnings.append(f'Team "{team.name}" roster is below roster_min ({settings.roster_min})')

    cancel_conflicting_open_items(moved_league_character_ids, warnings, exclude_trade_id=trade.id)
    trade.status = 'Executed'
    trade.date_resolved = int( time.time() )
    trade.resolved_by_comm_user_id = resolver_comm_user_id
    db.session.add(trade)
    db.session.commit()
    return warnings

def validate_lineup_slots(in_slots, team):
    # Validates the 9-slot shape and returns list of parsed slot dicts
    if not isinstance(in_slots, list) or len(in_slots) != 9:
        abort(413, description='Lineup must contain exactly 9 slots')
    batting_orders = set()
    fielding_positions = set()
    league_character_ids = set()
    parsed_slots = list()
    for in_slot in in_slots:
        league_character = LeagueCharacter.query.filter_by(id=in_slot['league_character_id']).first()
        if league_character == None or league_character.league_team_id != team.id:
            abort(414, description=f"Character instance with id={in_slot['league_character_id']} is not on this team")
        batting_order = in_slot['batting_order']
        fielding_pos = in_slot['fielding_pos']
        if batting_order not in range(9) or fielding_pos not in cFIELDING_POSITIONS:
            abort(415, description='batting_order and fielding_pos must be 0-8')
        batting_hand = in_slot.get('batting_hand')
        fielding_hand = in_slot.get('fielding_hand')
        superstar = in_slot.get('superstar')
        if (batting_hand != None and batting_hand not in cHANDEDNESS) or (fielding_hand != None and fielding_hand not in cHANDEDNESS):
            abort(416, description='Handedness override must be 0 (Right) or 1 (Left)')
        batting_orders.add(batting_order)
        fielding_positions.add(fielding_pos)
        league_character_ids.add(league_character.id)
        parsed_slots.append({
            'league_character_id': league_character.id,
            'batting_order': batting_order,
            'fielding_pos': fielding_pos,
            'batting_hand': batting_hand,
            'fielding_hand': fielding_hand,
            'superstar': (superstar == 1) if superstar != None else None
        })
    if len(batting_orders) != 9 or len(fielding_positions) != 9 or len(league_character_ids) != 9:
        abort(417, description='Lineup must use each batting order, fielding position, and character exactly once')
    return parsed_slots

# === League settings ===

@app.route('/league/settings/create', methods=['POST'])
@jwt_required(optional=True)
def league_settings_create():
    in_tag_set_id = request.json['tag_set_id']
    in_roster_min = request.json['roster_min']
    in_roster_max = request.json['roster_max']
    in_require_move_approval = (request.json['require_move_approval'] == 1)
    in_require_trade_approval = (request.json['require_trade_approval'] == 1)

    tag_set, settings, comm = get_league(in_tag_set_id, require_settings=False)
    if settings != None:
        return abort(410, description='League settings already exist for this TagSet')
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not is_league_admin:
        return abort(411, description='User is not an admin of this community')
    if in_roster_min < 9:
        return abort(412, description='roster_min must be at least 9 (a full lineup)')
    if in_roster_max < in_roster_min:
        return abort(413, description='roster_max must be greater than or equal to roster_min')

    new_settings = LeagueSettings(tag_set.id, in_roster_min, in_roster_max,
                                  in_require_move_approval, in_require_trade_approval)
    db.session.add(new_settings)
    db.session.commit()
    return jsonify(new_settings.to_dict())

@app.route('/league/settings/update', methods=['POST'])
@jwt_required(optional=True)
def league_settings_update():
    in_tag_set_id = request.json['tag_set_id']

    tag_set, settings, comm = get_league(in_tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not is_league_admin:
        return abort(410, description='User is not an admin of this community')

    new_roster_min = request.json['roster_min'] if 'roster_min' in request.json else settings.roster_min
    new_roster_max = request.json['roster_max'] if 'roster_max' in request.json else settings.roster_max
    if new_roster_min < 9:
        return abort(411, description='roster_min must be at least 9 (a full lineup)')
    if new_roster_max < new_roster_min:
        return abort(412, description='roster_max must be greater than or equal to roster_min')

    settings.roster_min = new_roster_min
    settings.roster_max = new_roster_max
    if 'require_move_approval' in request.json:
        settings.require_move_approval = (request.json['require_move_approval'] == 1)
    if 'require_trade_approval' in request.json:
        settings.require_trade_approval = (request.json['require_trade_approval'] == 1)
    db.session.add(settings)
    db.session.commit()
    return jsonify(settings.to_dict())

@app.route('/league/settings/get', methods=['POST'])
@jwt_required(optional=True)
def league_settings_get():
    in_tag_set_id = request.json['tag_set_id']
    tag_set, settings, comm = get_league(in_tag_set_id)
    check_read_access(request, comm)
    return jsonify(settings.to_dict())

# === Character pool ===

@app.route('/league/character/create', methods=['POST'])
@jwt_required(optional=True)
def league_character_create():
    in_tag_set_id = request.json['tag_set_id']
    in_characters = request.json['characters']

    tag_set, settings, comm = get_league(in_tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not is_league_admin:
        return abort(410, description='User is not an admin of this community')

    # Case/punctuation-insensitive alias lookup
    alias_map = {lower_and_remove_nonalphanumeric(alias): char_id for alias, char_id in cCHAR_ALIASES.items()}

    # Validate the entire list before inserting anything
    validated_entries = list()
    for entry in in_characters:
        if 'char_id' in entry:
            char_id = entry['char_id']
        elif 'name' in entry:
            char_id = alias_map.get(lower_and_remove_nonalphanumeric(entry['name']))
            if char_id == None:
                return abort(411, description=f"Unknown character name={entry['name']}")
        else:
            return abort(411, description='Each character entry needs a char_id or name')
        character = Character.query.filter_by(char_id=char_id).first()
        if character == None:
            return abort(411, description=f'Unknown char_id={char_id}')
        copies = entry['copies'] if 'copies' in entry else 1
        if not isinstance(copies, int) or copies < 1:
            return abort(412, description='copies must be a positive integer')
        batting_hand = entry.get('batting_hand')
        fielding_hand = entry.get('fielding_hand')
        if (batting_hand != None and batting_hand not in cHANDEDNESS) or (fielding_hand != None and fielding_hand not in cHANDEDNESS):
            return abort(413, description='Handedness must be 0 (Right) or 1 (Left)')
        validated_entries.append({
            'character': character,
            'copies': copies,
            # Default to the character's natural stance from the game data
            'batting_hand': batting_hand if batting_hand != None else character.batting_stance,
            'fielding_hand': fielding_hand if fielding_hand != None else character.fielding_arm,
            'superstar': (entry.get('superstar') == 1),
            'value': entry.get('value')
        })

    # Assign copy numbers continuing from the current max per character
    next_copy_num = dict()
    created_league_characters = list()
    for entry in validated_entries:
        char_id = entry['character'].char_id
        if char_id not in next_copy_num:
            current_max = db.session.query(db.func.max(LeagueCharacter.copy_num)).filter(
                LeagueCharacter.tag_set_id == tag_set.id,
                LeagueCharacter.char_id == char_id).scalar()
            next_copy_num[char_id] = (current_max or 0) + 1
        for _ in range(entry['copies']):
            new_league_character = LeagueCharacter(tag_set.id, char_id, next_copy_num[char_id],
                                                   entry['batting_hand'], entry['fielding_hand'],
                                                   entry['superstar'], entry['value'])
            next_copy_num[char_id] += 1
            db.session.add(new_league_character)
            created_league_characters.append(new_league_character)
    db.session.commit()
    return jsonify({'characters': [league_character.to_dict() for league_character in created_league_characters]})

@app.route('/league/character/update', methods=['POST'])
@jwt_required(optional=True)
def league_character_update():
    in_league_character_id = request.json['league_character_id']

    league_character = LeagueCharacter.query.filter_by(id=in_league_character_id).first()
    if league_character == None:
        return abort(409, description=f'Could not find character instance with id={in_league_character_id}')
    tag_set, settings, comm = get_league(league_character.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not is_league_admin:
        return abort(410, description='User is not an admin of this community')

    if 'batting_hand' in request.json:
        if request.json['batting_hand'] not in cHANDEDNESS:
            return abort(411, description='Handedness must be 0 (Right) or 1 (Left)')
        league_character.batting_hand = request.json['batting_hand']
    if 'fielding_hand' in request.json:
        if request.json['fielding_hand'] not in cHANDEDNESS:
            return abort(411, description='Handedness must be 0 (Right) or 1 (Left)')
        league_character.fielding_hand = request.json['fielding_hand']
    if 'superstar' in request.json:
        league_character.superstar = (request.json['superstar'] == 1)
    if 'value' in request.json:
        league_character.value = request.json['value']
    db.session.add(league_character)
    db.session.commit()
    return jsonify(league_character.to_dict())

@app.route('/league/character/delete', methods=['POST'])
@jwt_required(optional=True)
def league_character_delete():
    in_league_character_id = request.json['league_character_id']

    league_character = LeagueCharacter.query.filter_by(id=in_league_character_id).first()
    if league_character == None:
        return abort(409, description=f'Could not find character instance with id={in_league_character_id}')
    tag_set, settings, comm = get_league(league_character.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not is_league_admin:
        return abort(410, description='User is not an admin of this community')

    if league_character.league_team_id != None:
        return abort(411, description='Cannot delete a character instance that is on a team')
    referenced = (LeagueTradeItem.query.filter_by(league_character_id=league_character.id).first() != None
                  or LeagueMove.query.filter_by(league_character_id=league_character.id).first() != None
                  or LeagueLineupSlot.query.filter_by(league_character_id=league_character.id).first() != None)
    if referenced:
        return abort(412, description='Cannot delete a character instance referenced by trades, moves, or lineups')

    db.session.delete(league_character)
    db.session.commit()
    return jsonify('Success')

@app.route('/league/character/list', methods=['POST'])
@jwt_required(optional=True)
def league_character_list():
    in_tag_set_id = request.json['tag_set_id']
    tag_set, settings, comm = get_league(in_tag_set_id)
    check_read_access(request, comm)

    league_character_query = LeagueCharacter.query.filter_by(tag_set_id=tag_set.id)
    if request.json.get('free_agents') == True or request.json.get('free_agents') == 1:
        league_character_query = league_character_query.filter_by(league_team_id=None)
    if 'team_id' in request.json:
        league_character_query = league_character_query.filter_by(league_team_id=request.json['team_id'])

    return jsonify({'characters': [league_character.to_dict() for league_character in league_character_query]})

# === Teams ===

@app.route('/league/team/create', methods=['POST'])
@jwt_required(optional=True)
def league_team_create():
    in_tag_set_id = request.json['tag_set_id']
    in_name = request.json['name']
    in_logo_id = request.json['logo_id'] if 'logo_id' in request.json else None
    in_stadium_id = request.json['stadium_id'] if 'stadium_id' in request.json else None
    in_username = request.json['username'] if 'username' in request.json else None

    tag_set, settings, comm = get_league(in_tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)

    # Admins can create a team on behalf of another member
    if in_username != None:
        if not is_league_admin:
            return abort(410, description='Only league admins can create a team for another user')
        owner_rio_user = RioUser.query.filter_by(username_lowercase=lower_and_remove_nonalphanumeric(in_username)).first()
        if owner_rio_user == None:
            return abort(411, description=f'Could not find user with username={in_username}')
        owner_comm_user = CommunityUser.query.filter_by(user_id=owner_rio_user.id, community_id=comm.id).first()
    else:
        owner_comm_user = comm_user
    if owner_comm_user == None or not owner_comm_user.active or owner_comm_user.banned:
        return abort(412, description='Team owner is not an active member of this community')

    existing_team = LeagueTeam.query.filter_by(tag_set_id=tag_set.id, community_user_id=owner_comm_user.id).first()
    if existing_team != None:
        return abort(413, description='User already owns a team in this league')
    name_check = LeagueTeam.query.filter_by(tag_set_id=tag_set.id,
                                            name_lowercase=lower_and_remove_nonalphanumeric(in_name)).first()
    if name_check != None:
        return abort(414, description='Team name already in use in this league')
    if in_logo_id != None and in_logo_id not in cTEAM_LOGOS:
        return abort(415, description='Invalid logo_id')
    if in_stadium_id != None and in_stadium_id not in cSTADIUMS:
        return abort(416, description='Invalid stadium_id')

    new_team = LeagueTeam(tag_set.id, owner_comm_user.id, in_name, in_logo_id, in_stadium_id)
    db.session.add(new_team)
    db.session.commit()
    return jsonify(new_team.to_dict())

@app.route('/league/team/update', methods=['POST'])
@jwt_required(optional=True)
def league_team_update():
    in_team_id = request.json['team_id']

    team = get_team(in_team_id)
    tag_set, settings, comm = get_league(team.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not user_owns_team(comm_user, team) and not is_league_admin:
        return abort(410, description='User is not the owner of this team or a league admin')

    if 'name' in request.json:
        new_name = request.json['name']
        name_check = LeagueTeam.query.filter_by(tag_set_id=team.tag_set_id,
                                                name_lowercase=lower_and_remove_nonalphanumeric(new_name)).first()
        if name_check != None and name_check.id != team.id:
            return abort(411, description='Team name already in use in this league')
        team.name = new_name
        team.name_lowercase = lower_and_remove_nonalphanumeric(new_name)
    if 'logo_id' in request.json:
        if request.json['logo_id'] != None and request.json['logo_id'] not in cTEAM_LOGOS:
            return abort(412, description='Invalid logo_id')
        team.logo_id = request.json['logo_id']
    if 'stadium_id' in request.json:
        if request.json['stadium_id'] != None and request.json['stadium_id'] not in cSTADIUMS:
            return abort(413, description='Invalid stadium_id')
        team.stadium_id = request.json['stadium_id']
    if 'captain_league_character_id' in request.json:
        in_captain_id = request.json['captain_league_character_id']
        if in_captain_id == None:
            team.captain_league_character_id = None
        else:
            # Any character on the team may captain (captain-eligibility is not enforced)
            captain_character = LeagueCharacter.query.filter_by(id=in_captain_id).first()
            if captain_character == None or captain_character.league_team_id != team.id:
                return abort(414, description='Captain must be a character instance on this team')
            team.captain_league_character_id = in_captain_id

    db.session.add(team)
    db.session.commit()
    return jsonify(team.to_dict())

@app.route('/league/team/delete', methods=['POST'])
@jwt_required(optional=True)
def league_team_delete():
    in_team_id = request.json['team_id']

    team = get_team(in_team_id)
    tag_set, settings, comm = get_league(team.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not is_league_admin:
        return abort(410, description='Only league admins can delete a team')

    # Free all instances
    for league_character in LeagueCharacter.query.filter_by(league_team_id=team.id):
        league_character.league_team_id = None
        db.session.add(league_character)
    # Trade/move rows FK to the team, so they go with it (removes both sides' audit rows for those trades)
    trades = LeagueTrade.query.filter((LeagueTrade.from_team_id == team.id) | (LeagueTrade.to_team_id == team.id)).all()
    for trade in trades:
        LeagueTradeItem.query.filter_by(league_trade_id=trade.id).delete()
        db.session.delete(trade)
    LeagueMove.query.filter_by(league_team_id=team.id).delete()
    for lineup in LeagueLineup.query.filter_by(league_team_id=team.id):
        db.session.delete(lineup) #Cascades to slots
    db.session.delete(team)
    db.session.commit()
    return jsonify('Success')

@app.route('/league/team/get', methods=['POST'])
@jwt_required(optional=True)
def league_team_get():
    if 'team_id' in request.json:
        team = get_team(request.json['team_id'])
    else:
        in_tag_set_id = request.json['tag_set_id']
        tag_set, settings, comm = get_league(in_tag_set_id)
        if 'community_user_id' in request.json:
            owner_comm_user = CommunityUser.query.filter_by(id=request.json['community_user_id']).first()
        else:
            in_username = request.json['username']
            owner_rio_user = RioUser.query.filter_by(username_lowercase=lower_and_remove_nonalphanumeric(in_username)).first()
            if owner_rio_user == None:
                return abort(410, description=f'Could not find user with username={in_username}')
            owner_comm_user = CommunityUser.query.filter_by(user_id=owner_rio_user.id, community_id=comm.id).first()
        if owner_comm_user == None:
            return abort(410, description='User is not a member of this community')
        team = LeagueTeam.query.filter_by(tag_set_id=in_tag_set_id, community_user_id=owner_comm_user.id).first()
        if team == None:
            return abort(411, description='User does not have a team in this league')

    tag_set, settings, comm = get_league(team.tag_set_id)
    check_read_access(request, comm)

    ret_dict = team.to_dict(include_roster=True, include_lineups=True)
    ret_dict['owner_username'] = get_team_owner_username(team)
    ret_dict['roster_count'] = get_roster_count(team.id)
    ret_dict['roster_valid'] = get_roster_valid(team, settings)
    return jsonify(ret_dict)

@app.route('/league/team/list', methods=['POST'])
@jwt_required(optional=True)
def league_team_list():
    in_tag_set_id = request.json['tag_set_id']
    tag_set, settings, comm = get_league(in_tag_set_id)
    check_read_access(request, comm)

    team_list = list()
    for team in LeagueTeam.query.filter_by(tag_set_id=tag_set.id):
        team_dict = team.to_dict()
        team_dict['owner_username'] = get_team_owner_username(team)
        team_dict['roster_count'] = get_roster_count(team.id)
        team_list.append(team_dict)
    return jsonify({'teams': team_list})

# === Lineups ===

@app.route('/league/lineup/create', methods=['POST'])
@jwt_required(optional=True)
def league_lineup_create():
    in_team_id = request.json['team_id']
    in_name = request.json['name']
    in_is_default = ('is_default' in request.json and request.json['is_default'] == 1)
    in_slots = request.json['slots']

    team = get_team(in_team_id)
    tag_set, settings, comm = get_league(team.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not user_owns_team(comm_user, team) and not is_league_admin:
        return abort(410, description='User is not the owner of this team or a league admin')

    name_check = LeagueLineup.query.filter_by(league_team_id=team.id,
                                              name_lowercase=lower_and_remove_nonalphanumeric(in_name)).first()
    if name_check != None:
        return abort(411, description='Lineup name already in use for this team')
    parsed_slots = validate_lineup_slots(in_slots, team)

    # First lineup for a team becomes the default automatically
    has_lineups = LeagueLineup.query.filter_by(league_team_id=team.id).first() != None
    new_lineup = LeagueLineup(team.id, in_name, in_is_default or not has_lineups)
    if in_is_default:
        LeagueLineup.query.filter_by(league_team_id=team.id, is_default=True).update({'is_default': False})
    db.session.add(new_lineup)
    db.session.commit()
    for slot in parsed_slots:
        db.session.add(LeagueLineupSlot(new_lineup.id, slot['league_character_id'], slot['batting_order'],
                                        slot['fielding_pos'], slot['batting_hand'], slot['fielding_hand'],
                                        slot['superstar']))
    db.session.commit()
    return jsonify(new_lineup.to_dict())

@app.route('/league/lineup/update', methods=['POST'])
@jwt_required(optional=True)
def league_lineup_update():
    in_lineup_id = request.json['lineup_id']

    lineup = LeagueLineup.query.filter_by(id=in_lineup_id).first()
    if lineup == None:
        return abort(409, description=f'Could not find lineup with id={in_lineup_id}')
    team = get_team(lineup.league_team_id)
    tag_set, settings, comm = get_league(team.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not user_owns_team(comm_user, team) and not is_league_admin:
        return abort(410, description='User is not the owner of this team or a league admin')

    if 'name' in request.json:
        new_name = request.json['name']
        name_check = LeagueLineup.query.filter_by(league_team_id=team.id,
                                                  name_lowercase=lower_and_remove_nonalphanumeric(new_name)).first()
        if name_check != None and name_check.id != lineup.id:
            return abort(411, description='Lineup name already in use for this team')
        lineup.name = new_name
        lineup.name_lowercase = lower_and_remove_nonalphanumeric(new_name)
    if 'slots' in request.json:
        parsed_slots = validate_lineup_slots(request.json['slots'], team)
        LeagueLineupSlot.query.filter_by(league_lineup_id=lineup.id).delete()
        for slot in parsed_slots:
            db.session.add(LeagueLineupSlot(lineup.id, slot['league_character_id'], slot['batting_order'],
                                            slot['fielding_pos'], slot['batting_hand'], slot['fielding_hand'],
                                            slot['superstar']))
    db.session.add(lineup)
    db.session.commit()
    return jsonify(lineup.to_dict())

@app.route('/league/lineup/delete', methods=['POST'])
@jwt_required(optional=True)
def league_lineup_delete():
    in_lineup_id = request.json['lineup_id']

    lineup = LeagueLineup.query.filter_by(id=in_lineup_id).first()
    if lineup == None:
        return abort(409, description=f'Could not find lineup with id={in_lineup_id}')
    team = get_team(lineup.league_team_id)
    tag_set, settings, comm = get_league(team.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not user_owns_team(comm_user, team) and not is_league_admin:
        return abort(410, description='User is not the owner of this team or a league admin')

    db.session.delete(lineup) #Cascades to slots
    db.session.commit()
    return jsonify('Success')

@app.route('/league/lineup/set_default', methods=['POST'])
@jwt_required(optional=True)
def league_lineup_set_default():
    in_lineup_id = request.json['lineup_id']

    lineup = LeagueLineup.query.filter_by(id=in_lineup_id).first()
    if lineup == None:
        return abort(409, description=f'Could not find lineup with id={in_lineup_id}')
    team = get_team(lineup.league_team_id)
    tag_set, settings, comm = get_league(team.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not user_owns_team(comm_user, team) and not is_league_admin:
        return abort(410, description='User is not the owner of this team or a league admin')

    LeagueLineup.query.filter_by(league_team_id=team.id, is_default=True).update({'is_default': False})
    lineup.is_default = True
    db.session.add(lineup)
    db.session.commit()
    return jsonify(lineup.to_dict())

# === Roster moves (add/drop) ===

@app.route('/league/roster/add', methods=['POST'])
@jwt_required(optional=True)
def league_roster_add():
    return league_roster_move('Add')

@app.route('/league/roster/drop', methods=['POST'])
@jwt_required(optional=True)
def league_roster_drop():
    return league_roster_move('Drop')

def league_roster_move(move_type):
    in_team_id = request.json['team_id']
    in_league_character_id = request.json['league_character_id']

    team = get_team(in_team_id)
    tag_set, settings, comm = get_league(team.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not user_owns_team(comm_user, team) and not is_league_admin:
        return abort(410, description='User is not the owner of this team or a league admin')

    league_character = LeagueCharacter.query.filter_by(id=in_league_character_id).first()
    if league_character == None or league_character.tag_set_id != team.tag_set_id:
        return abort(411, description='Character instance not found in this league')
    if move_type == 'Add':
        if league_character.league_team_id != None:
            return abort(412, description='Character instance is not a free agent')
        if settings.roster_max != None and get_roster_count(team.id) + 1 > settings.roster_max:
            return abort(413, description='Team roster is full (roster_max reached)')
    else:
        if league_character.league_team_id != team.id:
            return abort(412, description='Character instance is not on this team')
    pending_move = LeagueMove.query.filter_by(league_character_id=league_character.id, status='Pending').first()
    if pending_move != None:
        return abort(414, description='Character instance already has a pending move')

    new_move = LeagueMove(team.tag_set_id, team.id, league_character.id, move_type)
    db.session.add(new_move)
    db.session.commit()

    warnings = list()
    if is_league_admin or not settings.require_move_approval:
        warnings = execute_move(new_move, settings, comm_user.id if comm_user != None else None)
    ret_dict = new_move.to_dict()
    ret_dict['warnings'] = warnings
    return jsonify(ret_dict)

@app.route('/league/move/list', methods=['POST'])
@jwt_required(optional=True)
def league_move_list():
    in_tag_set_id = request.json['tag_set_id']
    tag_set, settings, comm = get_league(in_tag_set_id)
    check_read_access(request, comm)

    move_query = LeagueMove.query.filter_by(tag_set_id=tag_set.id)
    if 'team_id' in request.json:
        move_query = move_query.filter_by(league_team_id=request.json['team_id'])
    if 'statuses' in request.json:
        move_query = move_query.filter(LeagueMove.status.in_(request.json['statuses']))

    return jsonify({'moves': [move.to_dict() for move in move_query]})

@app.route('/league/move/respond', methods=['POST'])
@jwt_required(optional=True)
def league_move_respond():
    in_move_id = request.json['move_id']
    in_accept = (request.json['accept'] == 1)

    move = LeagueMove.query.filter_by(id=in_move_id).first()
    if move == None:
        return abort(409, description=f'Could not find move with id={in_move_id}')
    tag_set, settings, comm = get_league(move.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not is_league_admin:
        return abort(410, description='Only league admins can respond to pending moves')
    if move.status != 'Pending':
        return abort(411, description=f'Move is not pending (status={move.status})')

    warnings = list()
    if in_accept:
        warnings = execute_move(move, settings, comm_user.id if comm_user != None else None)
    else:
        move.status = 'Rejected'
        move.date_resolved = int( time.time() )
        move.resolved_by_comm_user_id = comm_user.id if comm_user != None else None
        db.session.add(move)
        db.session.commit()
    ret_dict = move.to_dict()
    ret_dict['warnings'] = warnings
    return jsonify(ret_dict)

@app.route('/league/move/cancel', methods=['POST'])
@jwt_required(optional=True)
def league_move_cancel():
    in_move_id = request.json['move_id']

    move = LeagueMove.query.filter_by(id=in_move_id).first()
    if move == None:
        return abort(409, description=f'Could not find move with id={in_move_id}')
    team = get_team(move.league_team_id)
    tag_set, settings, comm = get_league(move.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not user_owns_team(comm_user, team) and not is_league_admin:
        return abort(410, description='User is not the owner of this team or a league admin')
    if move.status != 'Pending':
        return abort(411, description=f'Move is not pending (status={move.status})')

    move.status = 'Cancelled'
    move.date_resolved = int( time.time() )
    move.resolved_by_comm_user_id = comm_user.id if comm_user != None else None
    db.session.add(move)
    db.session.commit()
    return jsonify(move.to_dict())

# === Trades ===

@app.route('/league/trade/propose', methods=['POST'])
@jwt_required(optional=True)
def league_trade_propose():
    in_from_team_id = request.json['from_team_id']
    in_to_team_id = request.json['to_team_id']
    in_from_items = request.json['from_items']
    in_to_items = request.json['to_items']
    in_execute = ('execute' in request.json and request.json['execute'] == 1)

    from_team = get_team(in_from_team_id)
    to_team = get_team(in_to_team_id)
    if from_team.id == to_team.id:
        return abort(410, description='Cannot trade with the same team')
    if from_team.tag_set_id != to_team.tag_set_id:
        return abort(411, description='Teams are not in the same league')
    tag_set, settings, comm = get_league(from_team.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not user_owns_team(comm_user, from_team) and not is_league_admin:
        return abort(412, description='User is not the owner of the proposing team or a league admin')
    if in_execute and not is_league_admin:
        return abort(413, description='Only league admins can directly execute a trade')

    if len(in_from_items) + len(in_to_items) < 1:
        return abort(414, description='Trade must include at least one character')
    for league_character_id, giving_team in \
            [(i, from_team) for i in in_from_items] + [(i, to_team) for i in in_to_items]:
        league_character = LeagueCharacter.query.filter_by(id=league_character_id).first()
        if league_character == None or league_character.league_team_id != giving_team.id:
            return abort(415, description=f'Character instance with id={league_character_id} is not on team "{giving_team.name}"')
    if settings.roster_max != None:
        if get_roster_count(from_team.id) - len(in_from_items) + len(in_to_items) > settings.roster_max:
            return abort(416, description=f'Trade would put team "{from_team.name}" over roster_max')
        if get_roster_count(to_team.id) - len(in_to_items) + len(in_from_items) > settings.roster_max:
            return abort(416, description=f'Trade would put team "{to_team.name}" over roster_max')

    new_trade = LeagueTrade(from_team.tag_set_id, from_team.id, to_team.id)
    db.session.add(new_trade)
    db.session.commit()
    for league_character_id in in_from_items:
        db.session.add(LeagueTradeItem(new_trade.id, league_character_id, from_team.id))
    for league_character_id in in_to_items:
        db.session.add(LeagueTradeItem(new_trade.id, league_character_id, to_team.id))
    db.session.commit()

    warnings = list()
    if in_execute:
        warnings = execute_trade(new_trade, settings, comm_user.id if comm_user != None else None)
    ret_dict = new_trade.to_dict()
    ret_dict['warnings'] = warnings
    return jsonify(ret_dict)

@app.route('/league/trade/respond', methods=['POST'])
@jwt_required(optional=True)
def league_trade_respond():
    in_trade_id = request.json['trade_id']
    in_accept = (request.json['accept'] == 1)

    trade = LeagueTrade.query.filter_by(id=in_trade_id).first()
    if trade == None:
        return abort(409, description=f'Could not find trade with id={in_trade_id}')
    to_team = get_team(trade.to_team_id)
    tag_set, settings, comm = get_league(trade.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not user_owns_team(comm_user, to_team):
        return abort(410, description='Only the receiving team owner can respond to a trade')
    if trade.status != 'Proposed':
        return abort(411, description=f'Trade is not open (status={trade.status})')

    warnings = list()
    if not in_accept:
        trade.status = 'Rejected'
        trade.date_resolved = int( time.time() )
        trade.resolved_by_comm_user_id = comm_user.id
        db.session.add(trade)
        db.session.commit()
    elif settings.require_trade_approval:
        trade.status = 'Accepted' #Awaiting league admin approval
        db.session.add(trade)
        db.session.commit()
    else:
        warnings = execute_trade(trade, settings, comm_user.id)
    ret_dict = trade.to_dict()
    ret_dict['warnings'] = warnings
    return jsonify(ret_dict)

@app.route('/league/trade/cancel', methods=['POST'])
@jwt_required(optional=True)
def league_trade_cancel():
    in_trade_id = request.json['trade_id']

    trade = LeagueTrade.query.filter_by(id=in_trade_id).first()
    if trade == None:
        return abort(409, description=f'Could not find trade with id={in_trade_id}')
    from_team = get_team(trade.from_team_id)
    to_team = get_team(trade.to_team_id)
    tag_set, settings, comm = get_league(trade.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not user_owns_team(comm_user, from_team) and not user_owns_team(comm_user, to_team) and not is_league_admin:
        return abort(410, description='User is not part of this trade or a league admin')
    if trade.status not in ['Proposed', 'Accepted']:
        return abort(411, description=f'Trade is not open (status={trade.status})')

    trade.status = 'Cancelled'
    trade.date_resolved = int( time.time() )
    trade.resolved_by_comm_user_id = comm_user.id if comm_user != None else None
    db.session.add(trade)
    db.session.commit()
    return jsonify(trade.to_dict())

@app.route('/league/trade/execute', methods=['POST'])
@jwt_required(optional=True)
def league_trade_execute():
    in_trade_id = request.json['trade_id']

    trade = LeagueTrade.query.filter_by(id=in_trade_id).first()
    if trade == None:
        return abort(409, description=f'Could not find trade with id={in_trade_id}')
    tag_set, settings, comm = get_league(trade.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not is_league_admin:
        return abort(410, description='Only league admins can execute a trade')
    if trade.status not in ['Proposed', 'Accepted']:
        return abort(411, description=f'Trade is not open (status={trade.status})')

    warnings = execute_trade(trade, settings, comm_user.id if comm_user != None else None)
    ret_dict = trade.to_dict()
    ret_dict['warnings'] = warnings
    return jsonify(ret_dict)

@app.route('/league/trade/veto', methods=['POST'])
@jwt_required(optional=True)
def league_trade_veto():
    in_trade_id = request.json['trade_id']

    trade = LeagueTrade.query.filter_by(id=in_trade_id).first()
    if trade == None:
        return abort(409, description=f'Could not find trade with id={in_trade_id}')
    tag_set, settings, comm = get_league(trade.tag_set_id)
    user, comm_user, is_league_admin = get_acting_user(request, comm)
    if not is_league_admin:
        return abort(410, description='Only league admins can veto a trade')
    if trade.status not in ['Proposed', 'Accepted']:
        return abort(411, description=f'Trade is not open (status={trade.status})')

    trade.status = 'Vetoed'
    trade.date_resolved = int( time.time() )
    trade.resolved_by_comm_user_id = comm_user.id if comm_user != None else None
    db.session.add(trade)
    db.session.commit()
    return jsonify(trade.to_dict())

@app.route('/league/trade/list', methods=['POST'])
@jwt_required(optional=True)
def league_trade_list():
    in_tag_set_id = request.json['tag_set_id']
    tag_set, settings, comm = get_league(in_tag_set_id)
    check_read_access(request, comm)

    trade_query = LeagueTrade.query.filter_by(tag_set_id=tag_set.id)
    if 'team_id' in request.json:
        in_team_id = request.json['team_id']
        trade_query = trade_query.filter((LeagueTrade.from_team_id == in_team_id) | (LeagueTrade.to_team_id == in_team_id))
    if 'statuses' in request.json:
        trade_query = trade_query.filter(LeagueTrade.status.in_(request.json['statuses']))

    return jsonify({'trades': [trade.to_dict() for trade in trade_query]})

# === Mod-facing game load ===

@app.route('/league/game_load', methods=['GET'])
@jwt_required(optional=True)
def league_game_load():
    in_tag_set_id = request.args.get('tag_set_id')
    in_usernames = request.args.getlist('username')

    if in_tag_set_id == None or not in_tag_set_id.isdigit() or len(in_usernames) == 0:
        return abort(409, description='Provide a numeric tag_set_id and at least one username')
    tag_set, settings, comm = get_league(int(in_tag_set_id))
    check_read_access(request, comm)

    team_dicts = list()
    for in_username in in_usernames:
        rio_user = RioUser.query.filter_by(username_lowercase=lower_and_remove_nonalphanumeric(in_username)).first()
        if rio_user == None:
            return abort(410, description=f'Could not find user with username={in_username}')
        owner_comm_user = CommunityUser.query.filter_by(user_id=rio_user.id, community_id=comm.id).first()
        if owner_comm_user == None:
            return abort(410, description=f'User {in_username} is not a member of this community')
        team = LeagueTeam.query.filter_by(tag_set_id=tag_set.id, community_user_id=owner_comm_user.id).first()
        if team == None:
            return abort(410, description=f'User {in_username} does not have a team in this league')
        default_lineup = LeagueLineup.query.filter_by(league_team_id=team.id, is_default=True).first()
        if default_lineup == None or len(default_lineup.slots) != 9:
            return abort(411, description=f'Team "{team.name}" does not have a complete default lineup')
        if team.captain_league_character_id == None:
            return abort(412, description=f'Team "{team.name}" does not have a captain')

        captain_slot = None
        lineup_entries = list()
        for slot in sorted(default_lineup.slots, key=lambda slot: slot.batting_order):
            slot_dict = slot.to_dict()
            slot_dict['captain'] = (slot.league_character_id == team.captain_league_character_id)
            if slot_dict['captain']:
                captain_slot = slot
            lineup_entries.append(slot_dict)
        if captain_slot == None:
            return abort(412, description=f'Team "{team.name}" captain is not in the default lineup')

        team_dicts.append({
            'username': rio_user.username,
            'team_id': team.id,
            'team_name': team.name,
            'logo_id': team.logo_id,
            'stadium_id': team.stadium_id,
            'captain_league_character_id': team.captain_league_character_id,
            'captain_char_id': captain_slot.league_character.char_id,
            'captain_roster_loc': captain_slot.batting_order,
            'lineup': lineup_entries
        })

    return jsonify({'tag_set_id': tag_set.id, 'teams': team_dicts})
