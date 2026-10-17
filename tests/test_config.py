"""config 纯函数测试（分类目录说明注记 / category_names 解析）。"""

from app.config import (
    CATEGORY_DESCRIPTIONS,
    CategoryConfig,
    DEFAULT_GAME_CATEGORIES,
    describe_category,
)


class TestDescribeCategory:
    def test_known_category_annotated(self):
        assert describe_category("ForCharactersLoading") == (
            "ForCharactersLoading（角色加载用）"
        )
        assert describe_category("ForStudioScenesLoading") == (
            "ForStudioScenesLoading（工作室场景加载用）"
        )

    def test_unknown_category_untouched(self):
        assert describe_category("MyCustomDir") == "MyCustomDir"

    def test_surrounding_whitespace_stripped_for_lookup(self):
        assert describe_category(" MyMods ") == "MyMods（我的 Mod）"

    def test_default_categories_all_described(self):
        """默认清单里的每个分类都必须有中文说明（新增默认分类须同步映射表）"""
        names = DEFAULT_GAME_CATEGORIES.split(";")
        assert names and all(n in CATEGORY_DESCRIPTIONS for n in names)


class TestCategoryNames:
    def test_split_strip_dedupe_in_order(self):
        cfg = CategoryConfig(game_categories=" A ; B;;A; C ")
        assert cfg.category_names == ["A", "B", "C"]

    def test_default_not_empty(self):
        assert CategoryConfig().category_names
