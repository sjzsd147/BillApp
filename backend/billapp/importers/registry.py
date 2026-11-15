"""Importer Registry for automatic statement detection and dispatch."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from billapp.importers.alipay import AlipayImporter
from billapp.importers.base import FileProbe, Importer
from billapp.importers.boc_debit import BOCDebitImporter
from billapp.importers.icbc_credit import ICBCCreditImporter
from billapp.importers.wechat import WeChatImporter


class ImporterRegistry:
    """Registry managing available statement importers and dispatch."""

    def __init__(self) -> None:
        self._importers: Dict[str, Importer] = {}

    def register(self, importer: Importer) -> None:
        self._importers[importer.source_id] = importer

    def get(self, source_id: str) -> Optional[Importer]:
        return self._importers.get(source_id)

    def list_importers(self) -> List[Importer]:
        return list(self._importers.values())

    def detect_importer(
        self, probe: FileProbe, min_confidence: float = 0.5
    ) -> Tuple[Optional[Importer], float]:
        """Detect the best matching importer for a given file probe."""
        best_importer: Optional[Importer] = None
        best_score = 0.0

        for importer in self._importers.values():
            try:
                score = importer.detect(probe)
                if score > best_score and score >= min_confidence:
                    best_score = score
                    best_importer = importer
            except Exception:
                continue

        return best_importer, best_score


_GLOBAL_REGISTRY: Optional[ImporterRegistry] = None


def get_registry() -> ImporterRegistry:
    """Get the global default importer registry with built-in parsers."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = ImporterRegistry()
        _GLOBAL_REGISTRY.register(AlipayImporter())
        _GLOBAL_REGISTRY.register(WeChatImporter())
        _GLOBAL_REGISTRY.register(BOCDebitImporter())
        _GLOBAL_REGISTRY.register(ICBCCreditImporter())
    return _GLOBAL_REGISTRY
