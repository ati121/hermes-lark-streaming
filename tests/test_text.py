"""text.py 测试 — reasoning 标签解析与 TextState 状态追踪."""

from __future__ import annotations

from hermes_lark_streaming.state.text import (
    ReasoningStreamSplitter,
    TextState,
    extract_thinking_content,
    split_reasoning_text,
    strip_reasoning_tags,
)


class TestSplitReasoningText:
    def test_none_returns_empty(self) -> None:
        assert split_reasoning_text(None) == {}

    def test_empty_string_returns_empty(self) -> None:
        assert split_reasoning_text("") == {}

    def test_whitespace_only_returns_empty(self) -> None:
        assert split_reasoning_text("   \n  ") == {}

    def test_plain_text_no_tags(self) -> None:
        assert split_reasoning_text("Hello world") == {"answer_text": "Hello world"}

    def test_reasoning_prefix(self) -> None:
        result = split_reasoning_text("Reasoning:\nstep 1\nstep 2")
        assert result.keys() == {"reasoning_text"}
        assert "step 1" in result["reasoning_text"]

    def test_reasoning_prefix_strips_underscore_lines(self) -> None:
        result = split_reasoning_text("Reasoning:\n_thinking_\ndone")
        assert "_thinking_" not in (result.get("reasoning_text") or "")

    def test_reasoning_prefix_too_short_ignored(self) -> None:
        # "Reasoning:\n" 单独存在不比前缀长，应走普通文本逻辑
        assert split_reasoning_text("Reasoning:\n") == {"answer_text": "Reasoning:\n"}

    def test_thinking_tags(self) -> None:
        text = "<thinking>deep thoughts</thinking>answer here"
        result = split_reasoning_text(text)
        assert result["reasoning_text"] == "deep thoughts"
        # strip_reasoning_tags 移除标签但保留标签间内容
        assert "answer here" in result["answer_text"]

    def test_thought_tags(self) -> None:
        text = "<thought>reasoning</thought>the answer"
        result = split_reasoning_text(text)
        assert result["reasoning_text"] == "reasoning"
        assert "the answer" in result["answer_text"]

    def test_antthinking_tags(self) -> None:
        text = "<antthinking>model thoughts</antthinking>response"
        result = split_reasoning_text(text)
        assert result["reasoning_text"] == "model thoughts"
        assert "response" in result["answer_text"]

    def test_tags_with_whitespace(self) -> None:
        text = "< thinking >content< /thinking >rest"
        result = split_reasoning_text(text)
        assert result["reasoning_text"] == "content"

    def test_unclosed_tag(self) -> None:
        text = "<thinking>ongoing reasoning"
        result = split_reasoning_text(text)
        assert result["reasoning_text"] == "ongoing reasoning"
        # 未闭合的思考不能漏进正文：整段都是思考，答案为空
        assert result["answer_text"] is None

    def test_unclosed_tag_after_answer_keeps_only_answer(self) -> None:
        result = split_reasoning_text("答案是 42。<think>再检查一下")
        assert result["reasoning_text"] == "再检查一下"
        assert result["answer_text"] == "答案是 42。"


class TestExtractThinkingContent:
    def test_empty_string(self) -> None:
        assert extract_thinking_content("") == ""

    def test_no_tags(self) -> None:
        assert extract_thinking_content("plain text") == ""

    def test_single_pair(self) -> None:
        assert extract_thinking_content("<thinking>hello</thinking>") == "hello"

    def test_multiple_pairs(self) -> None:
        text = "<thinking>part1</thinking>ignored<thinking>part2</thinking>"
        assert extract_thinking_content(text) == "part1part2"

    def test_unclosed_tag_extracts_till_end(self) -> None:
        assert extract_thinking_content("<thinking>rest of text") == "rest of text"

    def test_case_insensitive(self) -> None:
        assert extract_thinking_content("<THOUGHT>content</THOUGHT>") == "content"


class TestStripReasoningTags:
    def test_removes_tag_markers(self) -> None:
        # 标签被移除，但标签间内容保留
        result = strip_reasoning_tags("<thinking>content</thinking>")
        assert "<thinking>" not in result
        assert "</thinking>" not in result

    def test_mixed_text_keeps_surrounding(self) -> None:
        text = "before<thinking>inner</thinking>after"
        result = strip_reasoning_tags(text)
        assert "before" in result
        assert "after" in result
        # 标签标记被移除
        assert "<thinking>" not in result

    def test_no_tags_unchanged(self) -> None:
        assert strip_reasoning_tags("no tags here") == "no tags here"

    def test_reasoning_prefix_clears_all(self) -> None:
        result = strip_reasoning_tags("Reasoning:\nsome content")
        assert result.strip() == ""

    def test_unclosed_tag_drops_tail(self) -> None:
        # 流被截断时模型正在思考 —— 开标签后面的一切都是思考，不能进正文
        assert strip_reasoning_tags("answer <think>secret plan") == "answer "

    def test_unclosed_tag_after_closed_pair_drops_tail(self) -> None:
        assert strip_reasoning_tags("<think>a</think>ans<think>tail") == "ans"

    def test_stray_close_tag_removed(self) -> None:
        assert strip_reasoning_tags("hello</think> world") == "hello world"

    def test_unclosed_tag_only_returns_empty(self) -> None:
        assert strip_reasoning_tags("<think>plan only, never closed") == ""


class TestTextState:
    def test_initial_state(self) -> None:
        ts = TextState()
        assert ts.display_text == ""
        assert ts.completed_text == ""

    def test_on_partial_accumulates(self) -> None:
        ts = TextState()
        ts.on_partial("hello ")
        ts.on_partial("world")
        assert ts.display_text == "hello world"

    def test_on_partial_empty_ignored(self) -> None:
        ts = TextState()
        ts.on_partial("")
        assert ts.display_text == ""

    def test_on_deliver_first(self) -> None:
        ts = TextState()
        ts.on_deliver("first block")
        assert ts.completed_text == "first block"
        assert ts.accumulated == "first block"

    def test_on_deliver_appends_with_separator(self) -> None:
        ts = TextState()
        ts.on_deliver("first")
        ts.on_deliver("second")
        assert ts.completed_text == "first\n\nsecond"

    def test_on_deliver_strips_reasoning_tags(self) -> None:
        ts = TextState()
        ts.on_deliver("<thinking>reasoning</thinking>answer")
        assert "<thinking>" not in ts.completed_text

    def test_display_text_prefers_accumulated(self) -> None:
        # on_deliver 在 accumulated 为空时设置它，之后 on_partial 追加
        ts = TextState()
        ts.on_deliver("delivered")
        ts.on_partial("partial")
        assert ts.display_text == "deliveredpartial"

    def test_display_text_fallback_to_completed(self) -> None:
        ts = TextState()
        ts.on_deliver("delivered")
        ts.accumulated = ""
        assert ts.display_text == "delivered"

# v1.3.0 P1-04: removed test_is_dirty_tracking and test_is_dirty_with_explicit_text
# — the is_dirty() / mark_flushed() / last_flushed mechanism was dead code
# (replaced by UnifiedLinearState dirty flags in v1.1.0).


class TestReasoningStreamSplitter:
    """跨 chunk 思考标签拆分 — on_answer 逐块喂入，开合状态跨块保留."""

    @staticmethod
    def _feed(chunks: list[str | None]) -> tuple[str, str]:
        sp = ReasoningStreamSplitter()
        rsn: list[str] = []
        ans: list[str] = []
        for c in chunks:
            r, a = sp.split(c)
            rsn.append(r)
            ans.append(a)
        r, a = sp.flush()
        rsn.append(r)
        ans.append(a)
        return ("".join(rsn), "".join(ans))

    def test_plain_text_no_tags(self) -> None:
        rsn, ans = self._feed(["Hello ", "world"])
        assert (rsn, ans) == ("", "Hello world")

    def test_complete_pair_in_one_chunk(self) -> None:
        rsn, ans = self._feed(["<think>abc</think>答案"])
        assert (rsn, ans) == ("abc", "答案")

    def test_tag_pair_split_across_chunks(self) -> None:
        # 开标签与闭标签都劈在两块之间 — 逐块 split 时 abc/答案会互相泄漏
        rsn, ans = self._feed(["<thi", "nk>abc</th", "ink>答案"])
        assert (rsn, ans) == ("abc", "答案")

    def test_unclosed_tag_at_stream_end_goes_to_reasoning(self) -> None:
        # 全文语义：标签前的 "开头" 是正文，未闭合尾巴里的都是思考
        rsn, ans = self._feed(["开头", "<think>想一半"])
        assert (rsn, ans) == ("想一半", "开头")

    def test_pending_partial_tag_in_answer_flushes_to_answer(self) -> None:
        # "<th" 不是标签开头，是 "a < th" 这类正文的疑似头部 — flush 归还正文
        rsn, ans = self._feed(["hello <th"])
        assert (rsn, ans) == ("", "hello <th")

    def test_reasoning_prefix_single_chunk(self) -> None:
        rsn, ans = self._feed(["Reasoning:\nstep 1\nstep 2"])
        assert (rsn, ans) == ("step 1\nstep 2", "")

    def test_reasoning_prefix_split_across_chunks(self) -> None:
        rsn, ans = self._feed(["Reas", "oning:\nfoo"])
        assert (rsn, ans) == ("foo", "")

    def test_reasoning_prefix_exactly_one_chunk(self) -> None:
        # 前缀恰好整块到达、换行在下一块 — 不能把后面的正文当 answer
        rsn, ans = self._feed(["Reasoning:", "\nfoo"])
        assert (rsn, ans) == ("foo", "")

    def test_reasoning_without_newline_is_not_marker(self) -> None:
        # 全文语义："Reasoning:" 后没有换行就不算标记，留在正文里
        rsn, ans = self._feed(["Reasoning:content"])
        assert (rsn, ans) == ("", "Reasoning:content")

    def test_leading_whitespace_before_prefix(self) -> None:
        rsn, ans = self._feed(["  Reasoning:\nfoo"])
        assert (rsn, ans) == ("foo", "")

    def test_reasoning_prefix_is_per_chunk_not_sticky(self) -> None:
        # 与 on_answer 既有契约一致：前缀只作用于所在的块，
        # 之后的块是正文（速度窗口由它开启）
        rsn, ans = self._feed(["Reasoning:\nhidden", "visible"])
        assert (rsn, ans) == ("hidden", "visible")

    def test_mid_stream_prefix_candidate_diverging_flows_through(self) -> None:
        rsn, ans = self._feed(["答案一", "Reas", "onable text"])
        assert (rsn, ans) == ("", "答案一Reasonable text")

    def test_angle_brackets_in_prose_not_held(self) -> None:
        rsn, ans = self._feed(["a<t b>c", " I <3 u"])
        assert (rsn, ans) == ("", "a<t b>c I <3 u")

    def test_multiple_blocks_interleaved_across_chunks(self) -> None:
        rsn, ans = self._feed(["<think>A</think>X", "<think>B</th", "ink>Y"])
        assert (rsn, ans) == ("AB", "XY")

    def test_tag_with_inner_whitespace_split(self) -> None:
        rsn, ans = self._feed(["< think", " >c< /thinking >a"])
        assert (rsn, ans) == ("c", "a")

    def test_empty_and_none_chunks(self) -> None:
        rsn, ans = self._feed(["", None, "<think>a</think>", "", "b"])
        assert (rsn, ans) == ("a", "b")

    def test_reconstruction_invariant(self) -> None:
        # 除标签本身外，两路增量拼回应还原原始内容
        chunks = ["今天", "<think>想想</think>", "给", "出<th", "ink>中间</think>答案"]
        rsn, ans = self._feed(chunks)
        assert rsn == "想想中间"
        assert ans == "今天给出答案"
