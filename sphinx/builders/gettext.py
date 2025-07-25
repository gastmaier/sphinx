"""The MessageCatalogBuilder class."""

from __future__ import annotations

import operator
import os
import os.path
import time
from collections import defaultdict
from os import getenv, walk
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from docutils import nodes

from sphinx import addnodes, package_dir
from sphinx._cli.util.colour import bold
from sphinx.builders import Builder
from sphinx.errors import ThemeError
from sphinx.locale import __
from sphinx.util import logging
from sphinx.util.display import status_iterator
from sphinx.util.i18n import docname_to_domain
from sphinx.util.index_entries import split_index_msg
from sphinx.util.nodes import extract_messages, traverse_translatable_index
from sphinx.util.osutil import canon_path, ensuredir, relpath
from sphinx.util.tags import Tags
from sphinx.util.template import SphinxRenderer

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence
    from typing import Any, Literal

    from docutils.nodes import Element

    from sphinx.application import Sphinx
    from sphinx.util.i18n import CatalogInfo
    from sphinx.util.typing import ExtensionMetadata

DEFAULT_TEMPLATE_PATH = package_dir.joinpath('templates', 'gettext')

logger = logging.getLogger(__name__)


class Message:
    """An entry of translatable message."""

    __slots__ = 'text', 'locations', 'uuids'

    text: str
    locations: list[tuple[str, int]]
    uuids: list[str]

    def __init__(
        self, text: str, locations: list[tuple[str, int]], uuids: list[str]
    ) -> None:
        self.text = text
        self.locations = locations
        self.uuids = uuids

    def __repr__(self) -> str:
        return (
            'Message('
            f'text={self.text!r}, locations={self.locations!r}, uuids={self.uuids!r}'
            ')'
        )


class Catalog:
    """Catalog of translatable messages."""

    __slots__ = ('metadata',)

    def __init__(self) -> None:
        # msgid -> file, line, uid
        self.metadata: dict[str, list[tuple[str, int, str]]] = {}

    def add(self, msg: str, origin: Element | MsgOrigin) -> None:
        if not hasattr(origin, 'uid'):
            # Nodes that are replicated like todo don't have a uid,
            # however translation is also unnecessary.
            return
        msg_metadata = self.metadata.setdefault(msg, [])
        line = line if (line := origin.line) is not None else -1
        msg_metadata.append((origin.source or '', line, origin.uid))

    def __iter__(self) -> Iterator[Message]:
        for message, msg_metadata in self.metadata.items():
            positions = sorted(set(map(operator.itemgetter(0, 1), msg_metadata)))
            uuids = list(map(operator.itemgetter(2), msg_metadata))
            yield Message(text=message, locations=positions, uuids=uuids)

    @property
    def messages(self) -> list[str]:
        return list(self.metadata)


class MsgOrigin:
    """Origin holder for Catalog message origin."""

    __slots__ = 'source', 'line', 'uid'

    source: str
    line: int
    uid: str

    def __init__(self, source: str, line: int) -> None:
        self.source = source
        self.line = line
        self.uid = uuid4().hex

    def __repr__(self) -> str:
        return f'<MsgOrigin {self.source}:{self.line}; uid={self.uid!r}>'


class GettextRenderer(SphinxRenderer):
    def __init__(
        self,
        template_path: Sequence[str | os.PathLike[str]] | None = None,
        outdir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.outdir = outdir
        if template_path is None:
            super().__init__([DEFAULT_TEMPLATE_PATH])
        else:
            super().__init__([*template_path, DEFAULT_TEMPLATE_PATH])

        def escape(s: str) -> str:
            s = s.replace('\\', r'\\')
            s = s.replace('"', r'\"')
            return s.replace('\n', '\\n"\n"')

        # use texescape as escape filter
        self.env.filters['e'] = escape
        self.env.filters['escape'] = escape

    def render(self, filename: str, context: dict[str, Any]) -> str:
        def _relpath(s: str) -> str:
            return canon_path(relpath(s, self.outdir))

        context['relpath'] = _relpath
        return super().render(filename, context)


class I18nTags(Tags):
    """Dummy tags module for I18nBuilder.

    To ensure that all text inside ``only`` nodes is translated,
    this class always returns ``True`` regardless the defined tags.
    """

    def eval_condition(self, condition: Any) -> bool:
        return True


class I18nBuilder(Builder):
    """General i18n builder."""

    name = 'i18n'
    versioning_method = 'text'
    use_message_catalog = False

    def init(self) -> None:
        super().init()
        self.env.set_versioning_method(self.versioning_method, self.config.gettext_uuid)
        self.tags = self._app.tags = I18nTags()
        self.catalogs: defaultdict[str, Catalog] = defaultdict(Catalog)

    def get_target_uri(self, docname: str, typ: str | None = None) -> str:
        return ''

    def get_outdated_docs(self) -> set[str]:
        return self.env.found_docs

    def compile_catalogs(self, catalogs: set[CatalogInfo], message: str) -> None:
        return

    def write_doc(self, docname: str, doctree: nodes.document) -> None:
        catalog = self.catalogs[docname_to_domain(docname, self.config.gettext_compact)]

        for toctree in self.env.tocs[docname].findall(addnodes.toctree):
            for node, msg in extract_messages(toctree):
                node.uid = ''  # type: ignore[attr-defined]  # Hack UUID model
                catalog.add(msg, node)

        for node, msg in extract_messages(doctree):
            # Do not extract messages from within substitution definitions.
            if not _is_node_in_substitution_definition(node):
                catalog.add(msg, node)

        if 'index' in self.config.gettext_additional_targets:
            # Extract translatable messages from index entries.
            for node, entries in traverse_translatable_index(doctree):
                for entry_type, value, _target_id, _main, _category_key in entries:
                    for m in split_index_msg(entry_type, value):
                        catalog.add(m, node)


# If set, use the timestamp from SOURCE_DATE_EPOCH
# https://reproducible-builds.org/specs/source-date-epoch/
if (source_date_epoch := getenv('SOURCE_DATE_EPOCH')) is not None:
    timestamp = time.gmtime(float(source_date_epoch))
else:
    # determine timestamp once to remain unaffected by DST changes during build
    timestamp = time.localtime()
ctime = time.strftime('%Y-%m-%d %H:%M%z', timestamp)


def should_write(filepath: Path, new_content: str) -> bool:
    if not filepath.exists():
        return True
    try:
        with open(filepath, encoding='utf-8') as oldpot:
            old_content = oldpot.read()
        old_header_index = old_content.index('"POT-Creation-Date:')
        new_header_index = new_content.index('"POT-Creation-Date:')
        old_body_index = old_content.index('"PO-Revision-Date:')
        new_body_index = new_content.index('"PO-Revision-Date:')
        return (
            old_content[:old_header_index] != new_content[:new_header_index]
            or new_content[new_body_index:] != old_content[old_body_index:]
        )
    except ValueError:
        pass

    return True


def _is_node_in_substitution_definition(node: nodes.Node) -> bool:
    """Check "node" to test if it is in a substitution definition."""
    while node.parent:
        if isinstance(node, nodes.substitution_definition):
            return True
        node = node.parent
    return False


class MessageCatalogBuilder(I18nBuilder):
    """Builds gettext-style message catalogs (.pot files)."""

    name = 'gettext'
    epilog = __('The message catalogs are in %(outdir)s.')

    def init(self) -> None:
        super().init()
        self.create_template_bridge()
        self.templates.init(self)

    def _collect_templates(self) -> set[str]:
        template_files = set()
        for template_path in self.config.templates_path:
            tmpl_abs_path = self.srcdir / template_path
            for dirpath, _dirs, files in walk(tmpl_abs_path):
                for fn in files:
                    if fn.endswith('.html'):
                        filename = Path(dirpath, fn).as_posix()
                        template_files.add(filename)
        return template_files

    def _extract_from_template(self) -> None:
        files = list(self._collect_templates())
        files.sort()
        logger.info(bold(__('building [%s]: ')), self.name, nonl=True)
        logger.info(__('targets for %d template files'), len(files))

        extract_translations = self.templates.environment.extract_translations

        for template in status_iterator(
            files,
            __('reading templates... '),
            'purple',
            len(files),
            self.config.verbosity,
        ):
            try:
                with open(template, encoding='utf-8') as f:
                    context = f.read()
                for line, _meth, msg in extract_translations(context):
                    origin = MsgOrigin(source=template, line=line)
                    self.catalogs['sphinx'].add(msg, origin)
            except Exception as exc:
                msg = f'{template}: {exc!r}'
                raise ThemeError(msg) from exc

    def build(  # type: ignore[misc]
        self,
        docnames: Iterable[str] | None,
        summary: str | None = None,
        method: Literal['all', 'specific', 'update'] = 'update',
    ) -> None:
        self._extract_from_template()
        super().build(docnames, summary, method)

    def finish(self) -> None:
        super().finish()
        context = {
            'version': self.config.version,
            'copyright': self.config.copyright,
            'project': self.config.project,
            'last_translator': self.config.gettext_last_translator,
            'language_team': self.config.gettext_language_team,
            'ctime': ctime,
            'display_location': self.config.gettext_location,
            'display_uuid': self.config.gettext_uuid,
        }
        catalog: Catalog
        for textdomain, catalog in status_iterator(
            self.catalogs.items(),
            __('writing message catalogs... '),
            'darkgreen',
            len(self.catalogs),
            self.config.verbosity,
            operator.itemgetter(0),
        ):
            # noop if config.gettext_compact is set
            ensuredir(self.outdir / os.path.dirname(textdomain))

            context['messages'] = list(catalog)
            template_path = [
                self.srcdir / rel_path for rel_path in self.config.templates_path
            ]
            renderer = GettextRenderer(template_path, outdir=self.outdir)
            content = renderer.render('message.pot.jinja', context)

            pofn = self.outdir / f'{textdomain}.pot'
            if should_write(pofn, content):
                with open(pofn, 'w', encoding='utf-8') as pofile:
                    pofile.write(content)


def setup(app: Sphinx) -> ExtensionMetadata:
    app.add_builder(MessageCatalogBuilder)

    app.add_config_value(
        'gettext_compact', True, 'gettext', types=frozenset({bool, str})
    )
    app.add_config_value('gettext_location', True, 'gettext', types=frozenset({bool}))
    app.add_config_value('gettext_uuid', False, 'gettext', types=frozenset({bool}))
    app.add_config_value('gettext_auto_build', True, 'env', types=frozenset({bool}))
    app.add_config_value(
        'gettext_additional_targets',
        [],
        'env',
        types=frozenset({frozenset, list, set, tuple}),
    )
    app.add_config_value(
        'gettext_last_translator',
        'FULL NAME <EMAIL@ADDRESS>',
        'gettext',
        types=frozenset({str}),
    )
    app.add_config_value(
        'gettext_language_team',
        'LANGUAGE <LL@li.org>',
        'gettext',
        types=frozenset({str}),
    )

    return {
        'version': 'builtin',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
