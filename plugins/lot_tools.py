"""Lot operations backend; all manager UI is owned by the core."""
from unified_sdk import FrameworkPlugin, VERSION
TOPIC_ICON='📝'
API_VERSION=1
NAME="🧰 Инструменты лотов"
TOPIC=False

class Plugin(FrameworkPlugin):
    def __init__(self,ctx):
        super().__init__(ctx)
        self.runtime.provide('lot_tools',self)
    @property
    def mode(self):return self.ctx.app.fp.mode
    @property
    def lot_export_errors(self):return getattr(self.ctx.app.fp,'lot_export_errors',[])
    async def lots(self):return await self.ctx.app.fp.lots()
    async def lot_export(self,include_secrets=False):return await self.ctx.app.fp.lot_export(include_secrets=include_secrets)
    async def public_seller_export(self,seller_id):return await self.ctx.app.fp.public_seller_export(seller_id)
    async def lot_import_one(self,row):return await self.ctx.app.fp.lot_import_one(row)
    async def delete_lot(self,lot_id):return await self.ctx.app.fp.delete_lot(lot_id)
