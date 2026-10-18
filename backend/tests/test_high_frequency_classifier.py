import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import high_frequency_classifier as classifier


class HighFrequencyClassifierTests(unittest.TestCase):
    def _model_module(self, response, calls):
        module = types.ModuleType("app.services.model_annotation")
        module.ModelAnnotationError = classifier.ModelAnnotationError
        module.resolve_call_config = lambda: {
            "base_url": "https://example.test",
            "api_key": "test-key",
            "model": "test-deepseek-model",
        }

        def fake_call(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return response

        module._call_deepseek_flash = fake_call
        return module

    def _settings(self, cache_path):
        return types.SimpleNamespace(
            HIGH_FREQUENCY_CLASSIFICATION_ENABLED=True,
            HIGH_FREQUENCY_CLASSIFICATION_BATCH_SIZE=4,
            HIGH_FREQUENCY_CLASSIFICATION_TIMEOUT_SECONDS=10,
            HIGH_FREQUENCY_CLASSIFICATION_MAX_COMPLETION_TOKENS=1024,
            HIGH_FREQUENCY_CLASSIFICATION_CACHE_PATH=str(cache_path),
            HIGH_FREQUENCY_CLASSIFICATION_MODEL="",
            DEEPSEEK_BASE_URL="",
            DEEPSEEK_API_KEY="",
            GROUP_LLM_BASE_URL="",
            GROUP_LLM_API_KEY="",
        )

    def test_split_calls_resolved_deepseek_model_with_core_question_only(self):
        item = {
            "id": "ticket-1",
            "category": "手机",
            "level1": "基础问题",
            "level2": "部件判定",
            "core_problem": "电池状态如何核验，同时屏幕偏光异常如何判定",
        }
        model_id = hashlib.sha256(
            "\x1f".join(
                (
                    "atomic-question-split-v2",
                    "test-deepseek-model",
                    item["category"],
                    item["level1"],
                    item["level2"],
                    item["core_problem"],
                )
            ).encode("utf-8")
        ).hexdigest()
        calls = []
        response = {
            "items": [
                {
                    "id": model_id,
                    "questions": ["电池状态如何核验", "屏幕偏光异常如何判定"],
                    "needs_review": False,
                }
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "splits.json"
            with patch.object(
                classifier,
                "settings",
                self._settings(cache_path),
            ), patch.object(
                classifier,
                "_split_cache_path",
                return_value=cache_path,
            ), patch.dict(
                sys.modules,
                {
                    "app.services.model_annotation": self._model_module(
                        response,
                        calls,
                    )
                },
            ):
                result, error = classifier.split_ambiguous_questions(
                    [item, {**item, "id": "ticket-1b"}]
                )

        self.assertIsNone(error)
        self.assertEqual(
            result["ticket-1"]["questions"],
            ["电池状态如何核验", "屏幕偏光异常如何判定"],
        )
        self.assertEqual(
            result["ticket-1b"]["questions"],
            ["电池状态如何核验", "屏幕偏光异常如何判定"],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["model_override"], "test-deepseek-model")
        self.assertIn("core_problem", calls[0][0])
        self.assertNotIn("chat_content", calls[0][0])
        self.assertNotIn("judgment", calls[0][0])

    def test_model_response_with_unknown_id_is_not_cached_as_a_split(self):
        item = {
            "id": "ticket-2",
            "category": "耳机",
            "core_problem": "定位功能是否正常，同时解绑是否成功",
        }
        calls = []
        response = {
            "items": [
                {
                    "id": "unexpected-id",
                    "questions": ["定位功能如何判定"],
                    "needs_review": False,
                }
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "splits.json"
            with patch.object(
                classifier,
                "settings",
                self._settings(cache_path),
            ), patch.object(
                classifier,
                "_split_cache_path",
                return_value=cache_path,
            ), patch.dict(
                sys.modules,
                {
                    "app.services.model_annotation": self._model_module(
                        response,
                        calls,
                    )
                },
            ):
                result, error = classifier.split_ambiguous_questions([item])
            cache_written = cache_path.exists()

        self.assertEqual(result, {})
        self.assertIn("返回不完整", error)
        self.assertFalse(cache_written)

    def test_classification_uses_short_model_ids_and_maps_back_to_cache_keys(self):
        items = [
            {"category": "手机", "issue": "扬声器播放杂音如何判定"},
            {"category": "手机", "issue": "前置摄像头画面抖动如何判定"},
        ]
        calls = []

        def fake_call(prompt, **kwargs):
            payload = json.loads(prompt.rsplit("\n\n", 1)[-1])
            model_ids = [row["id"] for row in payload]
            calls.append((payload, kwargs))
            return {
                "items": [
                    {
                        "id": model_id,
                        "business_theme": "质检判定",
                        "standard_question": f"标准问题 {index}",
                    }
                    for index, model_id in enumerate(model_ids, 1)
                ]
            }

        module = types.ModuleType("app.services.model_annotation")
        module.ModelAnnotationError = classifier.ModelAnnotationError
        module.resolve_call_config = lambda: {
            "base_url": "https://example.test",
            "api_key": "test-key",
            "model": "test-deepseek-model",
        }
        module._call_deepseek_flash = fake_call

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "classifications.json"
            with patch.object(
                classifier,
                "settings",
                self._settings(cache_path),
            ), patch.dict(
                sys.modules,
                {"app.services.model_annotation": module},
            ):
                expected_keys = {
                    classifier._cache_key("手机", "扬声器播放杂音如何判定"),
                    classifier._cache_key("手机", "前置摄像头画面抖动如何判定"),
                }
                result, error = classifier.classify_questions(items)

        self.assertIsNone(error)
        self.assertEqual(len(calls), 2)
        self.assertTrue(
            all(
                [row["id"] for row in payload] == [
                    f"q-{index}" for index in range(1, len(payload) + 1)
                ]
                for payload, _ in calls
            )
        )
        self.assertEqual(
            set(result),
            expected_keys,
        )

    def test_laptop_component_aliases_are_classified_without_model_cache(self):
        items = [
            {"category": "笔记本", "issue": "是否需判第三方硬盘"},
            {"category": "笔记本", "issue": "内存条是否为原装品牌件"},
            {"category": "平板电脑", "issue": "图片中的磕碰如何判定严重程度"},
            {
                "category": "手机",
                "issue": "关于本机显示电池正品部件，但一根线工具读出电池异常如何勾选",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "classifications.json"
            with patch.object(
                classifier,
                "settings",
                self._settings(cache_path),
            ):
                result, uncached = classifier.get_cached_classifications(items)

        self.assertEqual(uncached, 0)
        self.assertEqual(
            result[classifier._cache_key("笔记本", "是否需判第三方硬盘")],
            {
                "business_theme": "笔记本配件品牌核验",
                "standard_question": "硬盘是否品牌、原装或第三方",
            },
        )
        self.assertEqual(
            result[classifier._cache_key("笔记本", "内存条是否为原装品牌件")],
            {
                "business_theme": "笔记本配件品牌核验",
                "standard_question": "内存是否品牌、原装或第三方",
            },
        )
        self.assertEqual(
            result[
                classifier._cache_key(
                    "平板电脑",
                    "图片中的磕碰如何判定严重程度",
                )
            ],
            {
                "business_theme": "平板电脑外观质检",
                "standard_question": "外壳磕碰/掉漆如何判定",
            },
        )
        self.assertEqual(
            result[
                classifier._cache_key(
                    "手机",
                    "关于本机显示电池正品部件，但一根线工具读出电池异常如何勾选",
                )
            ],
            {
                "business_theme": "电池检测",
                "standard_question": "本机显示正品部件但工具读出电池异常如何判定",
            },
        )

    def test_confirmed_semantic_variants_use_stable_standard_questions(self):
        cases = [
            (
                {"category": "手机", "issue": "手机电池健康值显示评估中如何判定"},
                "电池健康度显示评估中如何判定",
            ),
            (
                {"category": "手机", "issue": "防水标签变红是否判定为浸液"},
                "防水标签变红是否判定浸液",
            ),
            (
                {"category": "手机", "issue": "偏光检测异常能否判定屏幕拆修"},
                "偏光检测异常如何判定",
            ),
            (
                {"category": "笔记本", "issue": "现场无法确认具体型号和年份"},
                "笔记本具体型号如何核实",
            ),
            (
                {"category": "笔记本", "issue": "笔记本是否支持指纹功能如何查看"},
                "笔记本是否支持指纹识别如何确认",
            ),
            (
                {"category": "笔记本", "issue": "BIOS锁状态如何现场确认"},
                "笔记本BIOS锁状态如何确认",
            ),
            (
                {"category": "笔记本", "issue": "进入BIOS需要按哪个按键"},
                "笔记本进入BIOS需按哪个按键",
            ),
            (
                {
                    "category": "笔记本",
                    "issue": "如何进入Windows笔记本的BIOS，需要操作指导",
                },
                "笔记本进入BIOS需按哪个按键",
            ),
            (
                {"category": "笔记本", "issue": "转轴吱吱声是否属于异常"},
                "笔记本转轴异响如何判定",
            ),
            (
                {"category": "笔记本", "issue": "发视频询问转轴异常是否算异常"},
                "笔记本转轴异响如何判定",
            ),
            (
                {
                    "category": "笔记本",
                    "issue": "转轴位置存在机身缝隙但无异常异响如何判定",
                },
                "笔记本转轴缝隙如何判定",
            ),
            (
                {"category": "平板", "issue": "平板外壳有刻字能否回收"},
                "平板机身外观有刻字如何判定",
            ),
            (
                {"category": "耳机/耳麦", "issue": "定位查找功能是否正常如何判定"},
                "耳机定位查找功能正常如何判定",
            ),
            (
                {
                    "category": "耳机",
                    "issue": "AirPods Max定位（查找）功能正常如何判定",
                },
                "耳机定位查找功能正常如何判定",
            ),
        ]

        for item, expected in cases:
            with self.subTest(item=item):
                result = classifier._deterministic_classification(item)
                self.assertIsNotNone(result)
                self.assertEqual(result["standard_question"], expected)

        disk = classifier._deterministic_classification(
            {"category": "笔记本", "issue": "硬盘是否品牌、原装或第三方"}
        )
        memory = classifier._deterministic_classification(
            {"category": "笔记本", "issue": "内存是否品牌、原装或第三方"}
        )
        self.assertNotEqual(
            disk["standard_question"],
            memory["standard_question"],
        )
        distinct_cases = [
            {
                "category": "笔记本",
                "issue": "该硬盘是否为品牌硬盘并有无明确品牌型号",
            },
            {
                "category": "笔记本",
                "issue": "进一步追问该硬盘的具体型号是什么",
            },
            {
                "category": "笔记本",
                "issue": "随机充电器型号是否与机型标准功率匹配",
            },
            {
                "category": "笔记本",
                "issue": "BIOS序列号显示INVALID时该机能否回收并确认机型",
            },
            {
                "category": "笔记本",
                "issue": "屏幕横线如何判定，会话中未就型号进行核实",
            },
            {
                "category": "笔记本",
                "issue": "小型号标识缺失是否说明更换过外壳并应判换壳",
            },
            {
                "category": "耳机",
                "issue": "在查找中解除账号绑定如何操作",
            },
            {
                "category": "耳机",
                "issue": "根据定位截图判断耳机是否为仿冒产品",
            },
            {
                "category": "耳机",
                "issue": "查询充电盒小型号并上传查找功能截图确认是否正品",
            },
        ]
        for item in distinct_cases:
            with self.subTest(distinct=item):
                result = classifier._deterministic_classification(item)
                self.assertFalse(
                    result
                    and result["standard_question"]
                    in {
                        "笔记本具体型号如何核实",
                        "笔记本转轴异响如何判定",
                        "笔记本BIOS锁状态如何确认",
                        "耳机定位查找功能正常如何判定",
                    }
                )

    def test_incremental_prompt_requires_short_titles_and_subject_matched_theme(self):
        calls = []
        response = {
            "items": [
                {
                    "id": "q-1",
                    "business_theme": "网络通信",
                    "standard_question": "蜂窝网络可联网但不能通话如何判定",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "classifications.json"
            with patch.object(
                classifier,
                "settings",
                self._settings(cache_path),
            ), patch.dict(
                sys.modules,
                {
                    "app.services.model_annotation": self._model_module(
                        response,
                        calls,
                    )
                },
            ):
                result, error = classifier.classify_incremental_question(
                    {
                        "category": "平板电脑",
                        "issue": "LTE版平板可联网但无法拨打电话，蜂窝网络功能如何判定",
                        "level1": "",
                        "level2": "",
                    },
                    [],
                )

        self.assertIsNone(error)
        self.assertEqual(
            result["standard_question"],
            "蜂窝网络可联网但不能通话如何判定",
        )
        self.assertIn("8 到 28 个汉字", calls[0][0])
        self.assertIn("不得包含具体品牌、机型、年份、容量", calls[0][0])
        self.assertIn("必须反映当前原子问题的对象", calls[0][0])

    def test_incremental_classification_retries_incomplete_model_response_once(self):
        calls = []
        responses = [
            {"items": []},
            {
                "items": [
                    {
                        "id": "q-1",
                        "business_theme": "屏幕显示",
                        "standard_question": "屏幕点状异常如何判定",
                    }
                ]
            },
        ]
        module = self._model_module({}, calls)

        def fake_call(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return responses.pop(0)

        module._call_deepseek_flash = fake_call
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "classifications.json"
            with patch.object(
                classifier,
                "settings",
                self._settings(cache_path),
            ), patch.dict(
                sys.modules,
                {"app.services.model_annotation": module},
            ):
                result, error = classifier.classify_incremental_question(
                    {"category": "手机", "issue": "屏幕点状异常如何判定"},
                    [],
                )

        self.assertIsNone(error)
        self.assertEqual(result["standard_question"], "屏幕点状异常如何判定")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
