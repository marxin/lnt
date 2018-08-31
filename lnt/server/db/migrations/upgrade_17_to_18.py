# Version 4 of the database adds unit and unit_abbrev column to StatusField.

from sqlalchemy import Column, String

from lnt.server.db.migrations.util import introspect_table
from lnt.server.db.util import add_column

def upgrade(engine):
    unit = Column('unit', String(256))
    unit_abbrev = Column('unit_abbrev', String(256))
    add_column(engine, 'TestSuiteSampleFields', unit)
    add_column(engine, 'TestSuiteSampleFields', unit_abbrev)
