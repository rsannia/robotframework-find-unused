from dataclasses import dataclass
from typing import Any

from robocop.checkers import VisitorChecker
from robocop.utils import normalize_robot_name
from robot.api.parsing import (
    Keyword,
    KeywordCall,
    SuiteSetup,
    SuiteTeardown,
    TestSetup,
    TestTeardown,
    Token,
)
from robot.parsing.model.blocks import Block
from robot.running.arguments.argumentmapper import DefaultValue

from robotframework_find_unused.common.const import KeywordData
from robotframework_find_unused.visitors.library_import import LibraryData


@dataclass
class KeywordCallData:
    keyword: str
    args: tuple[str, ...]


class KeywordVisitor(VisitorChecker):
    """
    A Robocop visitor.

    Will never log a lint issue, unlike a normal Robocop visitor. We use it here
    as a convenient way of working with Robotframework files.

    Uses file exclusion from the Robocop config.

    Gathers keywords
    Counts keyword usage
    Counts keyword argument usage
    """

    keywords: dict[str, KeywordData] = {}
    downloaded_libraries: list[LibraryData] = {}
    normalized_keyword_names: set[str] = set()

    def __init__(
        self,
        custom_keywords: list[KeywordData],
        downloaded_library_keywords: list[LibraryData],
    ):
        for kw in custom_keywords:
            self.keywords[kw.normalized_name] = kw
            self.normalized_keyword_names.add(kw.normalized_name)

        self.downloaded_libraries = downloaded_library_keywords
        for lib in self.downloaded_libraries:
            self.normalized_keyword_names.update(lib.keyword_names_normalized)

    def visit_Keyword(self, node: Keyword):  # noqa: N802
        """Keyword definition"""
        keyword = self._get_keyword_data(node.name)
        keyword.returns = self._get_keyword_returns(node)

        return self.generic_visit(node)

    def visit_KeywordCall(self, node: KeywordCall):  # noqa: N802
        """Keyword call / Keyword use"""
        return_assign_token = node.get_token(Token.ASSIGN)

        self._count_keyword_call(
            node.keyword,
            node.args,
            return_value_assigned=(return_assign_token is not None),
        )

        return self.generic_visit(node)

    def visit_TestSetup(self, node: TestSetup):
        self._count_keyword_call(node.name, node.args)

        return self.generic_visit(node)

    def visit_SuiteSetup(self, node: SuiteSetup):
        self._count_keyword_call(node.name, node.args)

        return self.generic_visit(node)

    def visit_TestTeardown(self, node: TestTeardown):
        self._count_keyword_call(node.name, node.args)

        return self.generic_visit(node)

    def visit_SuiteTeardown(self, node: SuiteTeardown):
        self._count_keyword_call(node.name, node.args)

        return self.generic_visit(node)

    def _remove_lib_from_name(self, name: str) -> str:
        if "." in name:
            name = name.split(".", 1)[1]
        return name

    def _get_keyword_data(self, name: str):
        name = self._remove_lib_from_name(name)
        normalized_name = normalize_robot_name(name)

        if normalized_name not in self.keywords:
            # Found a previously unused:
            # - downloaded library keyword
            # - out-of-scope keyword
            # or found a:
            # - keyword with embedded argument

            if normalized_name in self.normalized_keyword_names:
                self._register_downloaded_library_keyword(name, normalized_name)
            else:
                self._register_unknown_keyword(name, normalized_name)

        return self.keywords[normalized_name]

    def _count_keyword_call(
        self,
        name: str,
        args: tuple[str],
        *,
        return_value_assigned: bool = False,
    ):
        """
        Count the keyword.

        For keywords that take other keywords as arguments: Recursively handle inner keyword.
        """
        keyword = self._get_keyword_data(name)
        keyword.use_count += 1

        if return_value_assigned:
            keyword.return_use_count += 1

        inner_keywords = self._get_keyword_reference_in_argument(args, keyword)
        for inner in inner_keywords:
            self._count_keyword_call(inner.keyword, inner.args)

        if keyword.argument_use_count == None:
            # This is a downloaded library keyword. We don't care about the args
            return
        self._count_keyword_call_args(keyword, args)

    def _get_keyword_reference_in_argument(
        self,
        args: tuple[str],
        keyword: KeywordData,
    ) -> list[KeywordCallData]:
        """
        Return keyword calls in the given arguments

        - Only considers known keywords
        - Only considers cases where the keyword name or argument name includes 'keyword'

        Returns a list of tuples where (inner_keywor_name, inner_keyword_arguments)
        """
        inner_keywords: list[KeywordCallData] = []
        for i, arg in enumerate(args):
            arg_name = None
            arg_val = arg
            if "=" in arg:
                # Is a named arg
                (arg_name, arg_val) = arg.split("=", 1)

            normalized_name = normalize_robot_name(arg_val)
            if normalized_name not in self.normalized_keyword_names:
                # arg val is not a known keyword name
                continue

            if "keyword" in keyword.normalized_name:
                inner_keywords.append(
                    KeywordCallData(keyword=arg_val, args=args[i + 1 :]),
                )
                continue

            if arg_name is None:
                # Argument is positional. Named arg can't get here.
                arg_name = self._get_keyword_arg_name_by_position_index(
                    keyword,
                    position_index=i,
                )
            if arg_name is None:
                continue

            arg_name = arg_name.lower()
            if "keyword" in arg_name:
                inner_keywords.append(KeywordCallData(keyword=arg, args=args[i + 1 :]))

        if len(inner_keywords) > 1:
            for i in range(1, len(inner_keywords)):
                cur = inner_keywords[i]
                prev = inner_keywords[i - 1]

                prev.args = self._get_deduped_arguments(
                    prev,
                    cur,
                )

        return inner_keywords

    def _get_keyword_arg_name_by_position_index(
        self,
        keyword: KeywordData,
        position_index: int,
    ) -> str | None:
        """
        Return the argument name at the given positional index.

        Does not consider named arguments.
        """
        if keyword.arguments is None:
            # We don't know anything about the defined keyword arguments
            return None

        if keyword.arguments.var_positional and position_index > len(
            keyword.arguments.positional,
        ):
            # @{varargs} used
            return keyword.arguments.var_positional

        if position_index < len(keyword.arguments.argument_names):
            return keyword.arguments.argument_names[position_index]

        return None

    def _get_deduped_arguments(
        self,
        input: KeywordCallData,
        duplicate_call: KeywordCallData,
    ) -> tuple[str]:
        """
        Deduplicates arguments of the first keyword call. Used to prevent counting a keyword
        multiple times.
        """
        args_to_remove = (duplicate_call.keyword, *duplicate_call.args)

        output = [*input.args]
        for i in range(len(args_to_remove)):
            remove_val = args_to_remove[-i - 1]
            if "=" in remove_val:
                (_, remove_val) = remove_val.split("=", 1)

            actual_val = output.pop()
            if "=" in actual_val:
                (_, actual_val) = actual_val.split("=", 1)

            if remove_val != actual_val:
                raise ValueError(
                    f"Expected list to end with '{remove_val}', but found '{actual_val}' instead",
                )

        return tuple(output)

    def _register_downloaded_library_keyword(self, name: str, normalized_name: str):
        """
        Register as a downloaded library keyword.
        """
        library = None
        for lib in self.downloaded_libraries:
            if normalized_name in lib.keyword_names_normalized:
                library = lib
                break

        if library == None:
            raise Exception(f"Can't find library for keyword '{name}'")

        library_keyword = None
        for kw in library.keywords:
            if kw.normalized_name == normalized_name:
                library_keyword = kw
                break

        if library_keyword == None:
            raise Exception(f"Can't find keyword '{name}' in library '{library.name}'")

        self.keywords[library_keyword.normalized_name] = library_keyword

    def _register_unknown_keyword(self, name: str, normalized_name: str):
        """
        Register as an unknown keyword with minimum data that does not look weird.
        """
        self.keywords[normalized_name] = KeywordData(
            name=name,
            normalized_name=normalized_name,
            argument_use_count=None,
            deprecated=None,
            private=False,
            use_count=0,
            returns=None,
            return_use_count=0,
            type="UNKNOWN",
            arguments=None,
            library="",
        )

    def _count_keyword_call_args(self, kw: KeywordData, call_args: tuple[str]):
        positional_args: list[str] = []
        named_args: list[tuple[str, Any]] = []

        for arg in call_args:
            if "=" in arg:
                (named_arg_name, named_arg_val) = arg.split("=", 1)
                if named_arg_name in kw.arguments.argument_names:
                    # It's a correct named argument
                    named_args.append((named_arg_name, named_arg_val))
                    continue

            positional_args.append(arg)

        (called_with_args, called_with_kwargs) = kw.arguments.map(
            positional_args,
            named_args,
            replace_defaults=False,
        )

        called_with_kwarg_names = [a[0] for a in called_with_kwargs]
        kw_arg_names = [a for a in kw.arguments.argument_names if a not in called_with_kwarg_names]

        if len(kw_arg_names) == 0:
            return

        for position, arg in enumerate(called_with_args):
            if isinstance(arg, DefaultValue):
                continue

            if position >= len(kw_arg_names):
                position = len(kw_arg_names) - 1

            arg_name = kw_arg_names[position]
            kw.argument_use_count[arg_name] += 1

        for name, val in called_with_kwargs:
            if isinstance(val, DefaultValue):
                continue
            kw.argument_use_count[name] += 1

    def _get_keyword_returns(self, node: Keyword) -> bool:
        """
        Return if keyword returns a value or not.

        A return must have an explicit value.
        """
        for token in node.body:
            if isinstance(token, Block):
                # Block like `IF`, `FOR`, etc. Crawl recursively
                block_returns = self._get_keyword_returns(token)
                if block_returns is True:
                    return True
                continue

            if token.type in (
                Token.RETURN,
                Token.RETURN_STATEMENT,
            ):
                # `RETURN` and `[Return]` syntax
                if token.get_token(Token.ARGUMENT) is not None:
                    return True
                continue

            if token.type == Token.KEYWORD:
                # Special return keywords `Return From Keyword` and `Return From Keyword If`
                called_keyword_name = token.get_token(Token.KEYWORD)
                keyword_name_normalized = normalize_robot_name(
                    called_keyword_name.value,
                )

                if keyword_name_normalized not in (
                    "returnfromkeyword",
                    "returnfromkeywordif",
                ):
                    continue

                argument_count = len(token.get_tokens(Token.ARGUMENT))
                if keyword_name_normalized == "returnfromkeyword" and argument_count >= 1:
                    return True
                if keyword_name_normalized == "returnfromkeywordif" and argument_count >= 2:
                    return True
                continue

        return False
