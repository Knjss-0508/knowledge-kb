import json
import subprocess
import unittest
from pathlib import Path


class EmbeddingLabFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[2] / "frontend" / "index.html"
        ).read_text(encoding="utf-8")
        cls.lab_section = cls.html.split(
            '<template v-if="embedding.tab===\'lab\'">',
            1,
        )[1].split(
            '<template v-if="embedding.tab===\'samples\'">',
            1,
        )[0]
        cls.run_method = cls.html.split(
            "runEmbeddingLab: function() {",
            1,
        )[1].split(
            "setLabPositive: function(item) {",
            1,
        )[0]
        cls.inline_script = cls.html.rsplit("<script>", 1)[1].split(
            "</script>",
            1,
        )[0]

    def run_frontend_behavior(self, body: str) -> None:
        harness = f"""
const source = {json.dumps(self.inline_script, ensure_ascii=False)};
let captured = null;
globalThis.window = {{KB_RUNTIME: {{apiBase: '', baseUrl: ''}}}};
globalThis.Vue = {{
  createApp: function(options) {{
    captured = options;
    return {{mount: function() {{ return {{}}; }}}};
  }}
}};
new Function(source)();
if (!captured || !captured.methods) throw new Error('Vue methods not captured');
const vm = Object.assign({{}}, captured.methods);
const assert = function(condition, message) {{
  if (!condition) throw new Error(message);
}};
{body}
"""
        result = subprocess.run(
            ["node", "-"],
            input=harness,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=(result.stderr or result.stdout),
        )

    def test_lab_only_displays_applicability_scope_filters(self):
        for input_id, label in (
            ("embedding-lab-category", "适用品类"),
            ("embedding-lab-brand", "品牌"),
            ("embedding-lab-model", "机型"),
        ):
            self.assertIn(f'for="{input_id}"', self.lab_section)
            self.assertIn(f'id="{input_id}"', self.lab_section)
            self.assertIn(label, self.lab_section)
        for field_name in (
            "applicableCategoryId",
            "applicableBrandId",
            "applicableModelId",
        ):
            self.assertIn(
                f'v-model="embedding.lab.{field_name}"',
                self.lab_section,
            )
        for old_label in ("知识来源", "业务类型", "知识分类"):
            self.assertNotIn(old_label, self.lab_section)

    def test_lab_scope_is_a_fail_closed_three_level_cascade(self):
        self.assertIn(
            ':disabled="embedding.lab.scopeLoading || '
            '!embedding.lab.applicableCategoryId"',
            self.lab_section,
        )
        self.assertIn(
            ':disabled="embedding.lab.scopeLoading || '
            '!embedding.lab.applicableBrandId"',
            self.lab_section,
        )
        self.assertIn(
            "!embedding.lab.applicableCategoryId",
            self.lab_section,
        )
        self.assertIn(
            "请先选择适用品类，召回实验室不会直接检索全库",
            self.run_method,
        )

    def test_lab_loads_all_scope_options_and_sends_only_scope_fields(self):
        self.assertIn("fetch(API + '/manhattan/cache'", self.html)
        for field_name in (
            "applicable_category_id",
            "applicable_brand_id",
            "applicable_model_id",
        ):
            self.assertIn(field_name, self.run_method)
        for old_field_name in (
            "knowledge_origin:",
            "business_type:",
            "category_id:",
            "applicable_category_ids:",
            "brand_ids:",
            "model_ids:",
        ):
            self.assertNotRegex(
                self.run_method,
                rf"(?m)^\s+{old_field_name}",
            )

    def test_scope_options_follow_category_and_brand_exactly(self):
        self.run_frontend_behavior(
            r"""
vm.embedding = {
  lab: {
    applicableCategoryId: '',
    applicableBrandId: '',
    applicableModelId: '',
    scopeOptions: {
      applicable_categories: [
        {categoryId: '119', categoryName: '平板电脑'},
        {categoryId: '120', categoryName: '手机'}
      ],
      brands_by_category: {
        '119': [
          {brandId: '10530', brandName: '苹果'},
          {brandId: '10001', brandName: '安卓品牌'}
        ],
        '120': [{brandId: '10530', brandName: '苹果'}]
      },
      models: [
        {modelId: '97519', modelName: 'iPad 10', categoryId: '119', brandId: '10530'},
        {modelId: '88001', modelName: '安卓平板', categoryId: '119', brandId: '10001'},
        {modelId: '99001', modelName: 'iPhone', categoryId: '120', brandId: '10530'}
      ]
    },
    requestSeq: 0,
    loading: false,
    searched: false,
    results: [],
    positive: null,
    negatives: [],
    resultQuery: ''
  }
};
assert(vm.embeddingLabBrandOptions().length === 0, 'brand must await category');
assert(vm.embeddingLabModelOptions().length === 0, 'model must await brand');

vm.embedding.lab.applicableCategoryId = '119';
assert(vm.embeddingLabBrandOptions().length === 2, 'category should limit brands');
vm.embedding.lab.applicableBrandId = '10530';
const models = vm.embeddingLabModelOptions();
assert(models.length === 1, 'category and brand should limit models');
assert(models[0].value === '97519', 'only matching model should remain');

vm.embedding.lab.applicableModelId = '97519';
vm.onEmbeddingLabCategoryChange();
assert(vm.embedding.lab.applicableBrandId === '', 'category change must clear brand');
assert(vm.embedding.lab.applicableModelId === '', 'category change must clear model');

vm.embedding.lab.applicableBrandId = '10530';
vm.embedding.lab.applicableModelId = '97519';
vm.onEmbeddingLabBrandChange();
assert(vm.embedding.lab.applicableModelId === '', 'brand change must clear model');
"""
        )


if __name__ == "__main__":
    unittest.main()
