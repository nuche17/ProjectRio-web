"""Add draft league tables (league_settings, league_team, league_character,
league_lineup, league_lineup_slot, league_trade, league_trade_item, league_move)

Revision ID: b83f19c2d4a1
Revises: de7d5b19d918
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b83f19c2d4a1'
down_revision = 'de7d5b19d918'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('league_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tag_set_id', sa.Integer(), nullable=False),
        sa.Column('roster_min', sa.Integer(), nullable=True),
        sa.Column('roster_max', sa.Integer(), nullable=True),
        sa.Column('require_move_approval', sa.Boolean(), nullable=True),
        sa.Column('require_trade_approval', sa.Boolean(), nullable=True),
        sa.Column('date_created', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['tag_set_id'], ['tag_set.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tag_set_id', name='league_settings_tag_set_id_key')
    )
    # league_team.captain_league_character_id and league_character.league_team_id
    # form a circular FK pair; the captain FK is added after both tables exist
    op.create_table('league_team',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tag_set_id', sa.Integer(), nullable=False),
        sa.Column('community_user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=True),
        sa.Column('name_lowercase', sa.String(length=100), nullable=True),
        sa.Column('logo_id', sa.Integer(), nullable=True),
        sa.Column('stadium_id', sa.Integer(), nullable=True),
        sa.Column('captain_league_character_id', sa.Integer(), nullable=True),
        sa.Column('date_created', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['tag_set_id'], ['tag_set.id'], ),
        sa.ForeignKeyConstraint(['community_user_id'], ['community_user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tag_set_id', 'community_user_id', name='league_team_tag_set_id_community_user_id_key'),
        sa.UniqueConstraint('tag_set_id', 'name_lowercase', name='league_team_tag_set_id_name_lowercase_key')
    )
    op.create_table('league_character',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tag_set_id', sa.Integer(), nullable=False),
        sa.Column('char_id', sa.Integer(), nullable=False),
        sa.Column('copy_num', sa.Integer(), nullable=False),
        sa.Column('league_team_id', sa.Integer(), nullable=True),
        sa.Column('batting_hand', sa.Integer(), nullable=True),
        sa.Column('fielding_hand', sa.Integer(), nullable=True),
        sa.Column('superstar', sa.Boolean(), nullable=True),
        sa.Column('value', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['tag_set_id'], ['tag_set.id'], ),
        sa.ForeignKeyConstraint(['char_id'], ['character.char_id'], ),
        sa.ForeignKeyConstraint(['league_team_id'], ['league_team.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tag_set_id', 'char_id', 'copy_num', name='league_character_tag_set_id_char_id_copy_num_key')
    )
    op.create_foreign_key('fk_league_team_captain_league_character', 'league_team',
                          'league_character', ['captain_league_character_id'], ['id'])
    op.create_table('league_lineup',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('league_team_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=True),
        sa.Column('name_lowercase', sa.String(length=100), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=True),
        sa.Column('date_created', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['league_team_id'], ['league_team.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('league_team_id', 'name_lowercase', name='league_lineup_league_team_id_name_lowercase_key')
    )
    op.create_table('league_lineup_slot',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('league_lineup_id', sa.Integer(), nullable=False),
        sa.Column('league_character_id', sa.Integer(), nullable=False),
        sa.Column('batting_order', sa.Integer(), nullable=True),
        sa.Column('fielding_pos', sa.Integer(), nullable=True),
        sa.Column('batting_hand', sa.Integer(), nullable=True),
        sa.Column('fielding_hand', sa.Integer(), nullable=True),
        sa.Column('superstar', sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(['league_lineup_id'], ['league_lineup.id'], ),
        sa.ForeignKeyConstraint(['league_character_id'], ['league_character.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('league_lineup_id', 'batting_order', name='league_lineup_slot_lineup_batting_order_key'),
        sa.UniqueConstraint('league_lineup_id', 'fielding_pos', name='league_lineup_slot_lineup_fielding_pos_key'),
        sa.UniqueConstraint('league_lineup_id', 'league_character_id', name='league_lineup_slot_lineup_character_key')
    )
    op.create_table('league_trade',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tag_set_id', sa.Integer(), nullable=False),
        sa.Column('from_team_id', sa.Integer(), nullable=False),
        sa.Column('to_team_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=True),
        sa.Column('date_created', sa.Integer(), nullable=True),
        sa.Column('date_resolved', sa.Integer(), nullable=True),
        sa.Column('resolved_by_comm_user_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['tag_set_id'], ['tag_set.id'], ),
        sa.ForeignKeyConstraint(['from_team_id'], ['league_team.id'], ),
        sa.ForeignKeyConstraint(['to_team_id'], ['league_team.id'], ),
        sa.ForeignKeyConstraint(['resolved_by_comm_user_id'], ['community_user.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('league_trade_item',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('league_trade_id', sa.Integer(), nullable=False),
        sa.Column('league_character_id', sa.Integer(), nullable=False),
        sa.Column('from_team_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['league_trade_id'], ['league_trade.id'], ),
        sa.ForeignKeyConstraint(['league_character_id'], ['league_character.id'], ),
        sa.ForeignKeyConstraint(['from_team_id'], ['league_team.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('league_move',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tag_set_id', sa.Integer(), nullable=False),
        sa.Column('league_team_id', sa.Integer(), nullable=False),
        sa.Column('league_character_id', sa.Integer(), nullable=False),
        sa.Column('move_type', sa.String(length=10), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=True),
        sa.Column('date_created', sa.Integer(), nullable=True),
        sa.Column('date_resolved', sa.Integer(), nullable=True),
        sa.Column('resolved_by_comm_user_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['tag_set_id'], ['tag_set.id'], ),
        sa.ForeignKeyConstraint(['league_team_id'], ['league_team.id'], ),
        sa.ForeignKeyConstraint(['league_character_id'], ['league_character.id'], ),
        sa.ForeignKeyConstraint(['resolved_by_comm_user_id'], ['community_user.id'], ),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    op.drop_table('league_move')
    op.drop_table('league_trade_item')
    op.drop_table('league_trade')
    op.drop_table('league_lineup_slot')
    op.drop_table('league_lineup')
    op.drop_constraint('fk_league_team_captain_league_character', 'league_team', type_='foreignkey')
    op.drop_table('league_character')
    op.drop_table('league_team')
    op.drop_table('league_settings')
