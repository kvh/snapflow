from typing import Sequence

from basis.core.conversion.converter import (
    ConversionCostLevel,
    Converter,
    StorageFormat,
)


# TODO
class DatabaseToDatabaseConverter(Converter):
    supported_input_formats: Sequence[StorageFormat] = []
    supported_output_formats: Sequence[StorageFormat] = []
