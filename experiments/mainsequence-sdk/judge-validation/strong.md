Use one shared SQLAlchemy metadata graph and stable repository-prefixed physical
names. A suitable design is:

```python
import datetime
import uuid

from mainsequence.meta_tables import (
    PlatformManagedMetaTable,
    schema_table_name,
    sqlalchemy_naming_convention,
)
from sqlalchemy import DateTime, Float, ForeignKey, MetaData, String, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=sqlalchemy_naming_convention())


ACCOUNT_TABLE = schema_table_name("portfolio_console", "account")
POSITION_TABLE = schema_table_name("portfolio_console", "position")


class Account(PlatformManagedMetaTable, Base):
    __tablename__ = ACCOUNT_TABLE
    __metatable_identifier__ = "portfolio_console.Account"
    __metatable_namespace__ = "portfolio_console"
    __metatable_description__ = (
        "One row per broker account; parent record for portfolio positions."
    )

    account_uid: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        info={
            "label": "Account UID",
            "description": "Stable identifier for one broker account.",
        },
    )
    display_label: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        info={
            "label": "Display Label",
            "description": "Human-readable label used to identify the account.",
        },
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        info={
            "label": "Created At",
            "description": "UTC time at which the broker account record was created.",
        },
    )


class Position(PlatformManagedMetaTable, Base):
    __tablename__ = POSITION_TABLE
    __metatable_identifier__ = "portfolio_console.Position"
    __metatable_namespace__ = "portfolio_console"
    __metatable_description__ = (
        "One row per broker account and instrument pair, containing its quantity."
    )

    account_uid: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(f"{ACCOUNT_TABLE}.account_uid", ondelete="CASCADE"),
        primary_key=True,
        info={
            "label": "Account UID",
            "description": "Broker account that owns this position.",
        },
    )
    instrument_uid: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        info={
            "label": "Instrument UID",
            "description": "Stable identifier of the instrument held by the account.",
        },
    )
    quantity: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        info={
            "label": "Quantity",
            "description": "Current number of instrument units held in the account.",
        },
    )
```

The composite `Position` primary key enforces exactly one row per
`(account_uid, instrument_uid)` pair. The foreign key makes the account
relationship explicit, and `ondelete="CASCADE"` is appropriate because the
requirements explicitly say a position must not outlive its account. Cascade
would not be a safe default without that lifecycle rule.

Both models must be included in the existing migration provider's
`metatable_models` in parent-before-child order, with `Base.metadata` as the
provider target metadata. The provider-owned Alembic revision/review/upgrade
lifecycle creates and evolves the tables and their foreign key. Application
code must not call `.register()` directly, and no migration should be generated
or applied as part of this design response.
