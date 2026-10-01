import pytest
from pydantic import ValidationError

from pdf_notion_mvp.contracts import FixtureInput


@pytest.mark.parametrize("mutation", ["duplicate", "version", "page", "box", "missing_source", "table", "extra", "nan"])
def test_invalid_ir_rejected(source, mutation):
    data = source.model_dump()
    doc = data["document"]
    if mutation == "duplicate": doc["blocks"][1]["block_id"] = "b1"
    elif mutation == "version": doc["blocks"][1]["source"]["version"] = "different"
    elif mutation == "page": doc["blocks"][1]["source"]["page"] = 3
    elif mutation == "box": doc["blocks"][1]["source"]["bbox"]["x1"] = 900
    elif mutation == "missing_source": del doc["blocks"][1]["source"]
    elif mutation == "table": doc["blocks"][2]["cells"] = [["one"], ["two", "columns"]]
    elif mutation == "extra": doc["blocks"][1]["unknown"] = "not accepted"
    elif mutation == "nan": doc["blocks"][1]["source"]["bbox"]["x1"] = float("nan")
    with pytest.raises(ValidationError): FixtureInput.model_validate(data)
