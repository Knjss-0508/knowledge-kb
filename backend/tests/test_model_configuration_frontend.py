import json
import subprocess
import unittest
from pathlib import Path


class ModelConfigurationFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[2] / "frontend" / "index.html"
        ).read_text(encoding="utf-8")
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

    def test_managed_origin_has_fallback_labels(self):
        self.assertGreaterEqual(
            self.html.count(
                "{value:'model_configuration', label:'机型配置信息'}"
            ),
            2,
        )
        self.assertIn(
            "model_configuration:'机型配置信息'",
            self.html,
        )

    def test_managed_origin_is_not_offered_for_manual_assignment(self):
        self.assertIn(
            "assignableKnowledgeOrigins(candidateReviews.form.knowledge_origin)",
            self.html,
        )
        self.assertIn(
            "assignableKnowledgeOrigins(fm.knowledgeOrigin)",
            self.html,
        )
        self.assertIn(
            "key !== 'model_configuration' || currentKey === key",
            self.html,
        )
        self.assertIn(
            "item.knowledge_origin === 'model_configuration'",
            self.html,
        )
        self.assertIn(
            "r.knowledge_origin === 'model_configuration'",
            self.html,
        )
        self.assertNotIn(
            '<option v-for="origin in knowledgeOrigins"',
            self.html,
        )

    def test_managed_origin_remains_available_in_list_filter(self):
        self.assertIn(
            'v-for="origin in knowledgeOrigins"',
            self.html,
        )
        self.assertIn(
            "@click=\"setFilter('knowledge_origin', origin.value)\"",
            self.html,
        )

    def test_model_configuration_scope_uses_dynamic_single_value_cascade(self):
        self.assertIn(
            'v-if="showManhattanFields() && !isModelConfigurationForm()"',
            self.html,
        )
        self.assertIn(
            "if (activeForm.knowledgeOrigin === 'model_configuration') return true;",
            self.html,
        )
        self.assertIn(
            "businessType: isModelConfiguration ? 'self_operated'",
            self.html,
        )
        self.assertIn(
            "if (self.fm.businessType) {\n        self.loadManhattanOptions(self.fm.businessType);",
            self.html,
        )
        for ref_name in (
            "modelConfigurationCategoryInput",
            "modelConfigurationBrandInput",
            "modelConfigurationModelInput",
        ):
            self.assertIn(ref_name, self.html)
        self.assertIn(
            "arr.splice(0, arr.length, value);",
            self.html,
        )
        self.assertIn(
            "this.modelConfigurationScopeFallbackLabel(key, value)",
            self.html,
        )

    def test_model_configuration_scope_has_no_free_text_id_or_name_inputs(self):
        for field_name in (
            "categoryId",
            "categoryName",
            "brandId",
            "brandName",
            "modelId",
            "modelName",
        ):
            self.assertNotIn(
                f'v-model="fm.modelConfiguration.{field_name}"',
                self.html,
            )
        self.assertNotIn(
            "onModelConfigurationScopeChange",
            self.html,
        )

    def test_model_configuration_only_displays_comprehensive_content(self):
        self.assertNotIn(
            '<div class="sec-t">个性属性</div>',
            self.html,
        )
        self.assertNotIn(
            "model-configuration-attribute-grid",
            self.html,
        )
        self.assertNotIn(
            'placeholder="留空表示无记录"',
            self.html,
        )
        self.assertIn(
            'v-model="fm.modelConfiguration.content"',
            self.html,
        )
        self.assertNotIn(
            "modelConfigurationAttributeNames",
            self.html,
        )
        self.assertNotIn(
            "modelConfiguration.attributes",
            self.html,
        )

    def test_model_configuration_save_requires_current_manhattan_options(self):
        self.assertIn(
            "var categorySelection = this.modelConfigurationScopeSelection('applicableCategories');",
            self.html,
        )
        self.assertIn(
            "if (!scopeSelections[scopeIndex][1].matched)",
            self.html,
        )
        self.assertIn(
            "已不在最新曼哈顿配置中，请重新选择后保存。",
            self.html,
        )
        self.assertIn(
            "category_name: categorySelection.name",
            self.html,
        )
        self.assertIn(
            "brand_name: brandSelection.name",
            self.html,
        )
        self.assertIn(
            "model_name: modelSelection.name",
            self.html,
        )

    def test_managed_cascade_is_fail_closed_until_each_parent_is_selected(self):
        self.assertIn(
            ':disabled="!canSelectManhattanField(\'brands\')"',
            self.html,
        )
        self.assertIn(
            ':disabled="!canSelectManhattanField(\'models\')"',
            self.html,
        )
        self.assertIn(
            "self.isModelConfigurationForm() && categoryIds.length !== 1",
            self.html,
        )
        self.assertIn(
            "&& (categoryIds.length !== 1 || brandIds.length !== 1)",
            self.html,
        )
        self.run_frontend_behavior(
            r"""
vm.fm = {
  knowledgeOrigin: 'model_configuration',
  businessType: 'self_operated',
  cat: 'cat-extra-knowledge',
  applicableCategories: [],
  brands: [],
  models: [],
  modelConfiguration: {}
};
vm.option = {
  applicableCategories: [],
  brands: [{label: 'stale brand', value: 'stale-brand'}],
  models: [{label: 'stale model', value: 'stale-model'}]
};
vm.optErr = {applicableCategories: '', brands: '', models: ''};
vm.mhCaches = {
  self_operated: {
    applicable_categories: [
      {categoryId: '119', categoryName: '平板电脑'}
    ],
    brands_by_category: {
      '119': [
        {brandId: '10530', brandName: '苹果'},
        {brandId: '10001', brandName: '安卓品牌'}
      ]
    },
    models: [
      {
        modelId: '97519',
        modelName: 'iPad 10',
        categoryId: '119',
        brandId: '10530'
      },
      {
        modelId: '88001',
        modelName: '安卓平板',
        categoryId: '119',
        brandId: '10001'
      }
    ]
  }
};
vm.cats = [];
vm.viewOnly = false;
vm.categoryKw = '';
vm.brandKw = '';
vm.modelKw = '';
vm.filterDrop = '';
vm.drop = '';
vm.$refs = {};
vm.$nextTick = function(callback) { callback(); };

vm.loadBrands();
assert(vm.option.brands.length === 0, 'brands must clear without category');
assert(vm.option.models.length === 0, 'models must clear without category');
assert(!vm.canSelectManhattanField('brands'), 'brand field must stay disabled');
assert(!vm.canSelectManhattanField('models'), 'model field must stay disabled');
vm.focusMultiInput('modelConfigurationBrandInput', 'brands');
assert(vm.drop === '', 'disabled brand field must not open');

vm.fm.applicableCategories = ['119'];
vm.loadBrands();
assert(vm.option.brands.length === 2, 'single category should load brands');
assert(vm.option.models.length === 0, 'models must remain empty without brand');
assert(vm.canSelectManhattanField('brands'), 'brand field should be enabled');
assert(!vm.canSelectManhattanField('models'), 'model field must await brand');
vm.focusMultiInput('modelConfigurationModelInput', 'models');
assert(vm.drop === '', 'disabled model field must not open');

vm.fm.brands = ['10530'];
vm.loadModels();
assert(vm.option.models.length === 1, 'single brand should scope models');
assert(vm.option.models[0].value === '97519', 'model must match selected brand');
assert(vm.canSelectManhattanField('models'), 'model field should be enabled');
vm.focusMultiInput('modelConfigurationModelInput', 'models');
assert(vm.drop === 'models', 'enabled model field should open');

vm.fm.brands = [];
vm.loadModels();
assert(vm.option.models.length === 0, 'models must clear after brand removal');
"""
        )


if __name__ == "__main__":
    unittest.main()
