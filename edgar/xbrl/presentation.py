import re
from collections import defaultdict, OrderedDict
from typing import Dict, List, Optional
from typing import Union

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ConfigDict
from rich import print as rprint
from rich.text import Text
from rich.tree import Tree

from edgar.richtools import repr_rich

__all__ = ['XBRLPresentation', 'PresentationElement', 'get_root_element', 'get_axes_for_role', 'get_members_for_axis']


class PresentationElement:
    """
    A single node in the presentation linkbase hierarchy.
    """

    def __init__(self, label: str, href: str, order: float, concept: str = None, preferred_label: str = None):
        self.label = label
        self.href = href
        self.order = order
        self.concept = concept if concept else label  # Use label as fallback if concept is not provided
        self.level = 0
        self.children = []
        self.preferred_label = preferred_label
        self.parent = None

    @property
    def node_type(self) -> str:
        """
        Determine the type of node based on the concept name and common XBRL patterns.

        Returns:
            str: The type of node ('Statement', 'Axis', 'Member', 'Domain', 'Table',
                 'Abstract', 'LineItems', or 'LineItem')
        """
        # Special case for statement root elements (role URIs)
        if self.concept.startswith('http://') and 'role' in self.concept.lower():
            return 'Statement'

        concept_lower = self.concept.lower()

        # Check for Table patterns
        if self.concept.endswith('Table') or 'statementtable' in concept_lower:
            return 'Table'

        # Check for LineItems patterns
        if (self.concept.endswith('LineItems') or
                'statementlineitems' in concept_lower or
                'schedulelineitems' in concept_lower):
            return 'LineItems'

        # Check for Axis patterns
        if self.concept.endswith('Axis') or '[axis]' in concept_lower:
            return 'Axis'

        # Check for Member patterns
        if self.concept.endswith('Member') or '[member]' in concept_lower:
            return 'Member'

        # Check for Domain patterns
        if self.concept.endswith('Domain') or '[domain]' in concept_lower:
            return 'Domain'

        # Check for Abstract patterns
        if (self.concept.endswith('Abstract') or
                '[abstract]' in concept_lower or
                'abstract' in concept_lower):
            return 'Abstract'

        # Default case
        return 'LineItem'


    def is_abstract(self):
        return self.concept.endswith('Abstract')

    def __repr__(self):
        return f"PresentationElement(label='{self.label}', concept='{self.concept}', children={len(self.children)})"


class XBRLPresentation(BaseModel):
    # Dictionary to store presentation roles and their corresponding elements
    roles: Dict[str, PresentationElement] = Field(default_factory=dict)
    skipped_roles: List[str] = Field(default_factory=list)
    standard_statement_map: Dict[str, str] = Field(default_factory=dict)
    concept_index: Dict[str, List[str]] = Field(default_factory=lambda: defaultdict(list))

    # Configuration to allow arbitrary types in the model
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @classmethod
    def parse(cls, xml_string: str):
        presentation = cls()
        soup = BeautifulSoup(xml_string, 'xml')

        def normalize_concept(concept):
            return re.sub(r'_\d+$', '', concept)

        for plink in soup.find_all(['presentationLink', 'link:presentationLink']):
            role = plink.get('xlink:role') or plink.get('role')
            if not role:
                continue

            # Parse loc elements
            locs = OrderedDict()
            for loc in plink.find_all(['loc', 'link:loc']):
                label = loc.get('xlink:label') or loc.get('label')
                href = loc.get('xlink:href') or loc.get('href')
                if not label or not href:
                    continue
                concept = href.split('#')[-1]
                normalized_concept = normalize_concept(concept)
                locs[label] = PresentationElement(label=label, href=href, order=0, concept=normalized_concept)

            # Parse presentationArc elements
            arcs = []
            for arc in plink.find_all(['presentationArc', 'link:presentationArc']):
                parent_label = arc.get('xlink:from') or arc.get('from')
                child_label = arc.get('xlink:to') or arc.get('to')
                order = float(arc.get('order', '0'))
                preferred_label = arc.get('preferredLabel')
                arcs.append((parent_label, child_label, order, preferred_label))

            # If no loc elements were found, try to parse using the older format
            if not locs:
                for arc in arcs:
                    parent_label, child_label, order, _ = arc
                    if parent_label not in locs:
                        normalized_concept = normalize_concept(parent_label)
                        locs[parent_label] = PresentationElement(label=parent_label, href='', order=0,
                                                                 concept=normalized_concept)
                    if child_label not in locs:
                        normalized_concept = normalize_concept(child_label)
                        locs[child_label] = PresentationElement(label=child_label, href='', order=0,
                                                                concept=normalized_concept)

            # Build the hierarchy
            for parent_label, child_label, order, preferred_label in arcs:
                if parent_label in locs and child_label in locs:
                    parent = locs[parent_label]
                    child = locs[child_label]
                    child.order = order
                    child.level = parent.level + 1
                    child.preferred_label = preferred_label

                    existing_child = next((c for c in parent.children if
                                           normalize_concept(c.concept) == normalize_concept(child.concept)), None)
                    if existing_child:
                        existing_child.children.extend(child.children)
                        if len(child.label) > len(existing_child.label):
                            existing_child.label = child.label
                        if len(child.concept) < len(existing_child.concept):
                            existing_child.concept = child.concept
                        if child.preferred_label:
                            existing_child.preferred_label = child.preferred_label
                    else:
                        parent.children.append(child)
                        child.parent = parent

            # Add the top-level elements to the role only if there are children
            role_children = [loc for loc in locs.values() if not any(arc[1] == loc.label for arc in arcs)]
            if role_children:
                presentation.roles[role] = PresentationElement(label=role, href='', order=0,
                                                               concept=normalize_concept(role))
                # Set the parent of each child to the role element
                for role_child in role_children:
                    role_child.parent = presentation.roles[role]
                presentation.roles[role].children = role_children

        # Build the statement map and concept index
        presentation._build_statement_map()
        presentation._build_concept_index()

        return presentation

    def _build_statement_map(self):
        for role, element in self.roles.items():
            standard_name = FinancialStatementMapper.get_standard_name(role)
            if standard_name:
                self.standard_statement_map[standard_name] = role

    def _build_concept_index(self):
        for role, element in self.roles.items():
            self._index_concepts(element, role)

    def _index_concepts(self, element: PresentationElement, role: str):
        self.concept_index[element.concept].append(role)
        for child in element.children:
            self._index_concepts(child, role)

    def get_role_by_standard_name(self, standard_name: str) -> Optional[str]:
        return self.standard_statement_map.get(standard_name)

    def get_roles_containing_concept(self, concept: str) -> List[str]:
        if '_' not in concept:
            namespaces = ['us-gaap', 'ifrs-full', 'dei']  # Add other common namespaces as needed
            for ns in namespaces:
                namespaced_concept = f"{ns}_{concept}"
                if namespaced_concept in self.concept_index:
                    return self.concept_index[namespaced_concept]

            # If the concept is already namespaced or not found with common namespaces
        return self.concept_index.get(concept, [])

    def get_axes_for_role(self, role: Union[str, PresentationElement]) -> List[PresentationElement]:
        """
        Find all axes (dimensions) for a given role.

        Args:
            role: Either a role name (str) or a PresentationElement

        Returns:
            List of axis names
        """
        if role in self.roles:
            return get_axes_for_role(self.roles[role])

    def get_members_for_axis(self, axis: PresentationElement) -> List[str]:
        """
        List all members for a given axis in a specific role.

        Args:
            axis: The axis to find members for

        Returns:
            List of member names
        """
        return get_members_for_axis(axis)

    def get_statement_line_items(self, role: Union[str, PresentationElement]) -> List[PresentationElement]:
        """
        Get all presentation elements that fall under StatementLineItems for a given role.

        Args:
            role: Either a role name (str) or a PresentationElement

        Returns:
            List of PresentationElements under StatementLineItems
        """
        line_items = []

        def find_line_items(element):
            if element.node_type == 'Table':
                for child in element.children:
                    if child.node_type == 'LineItems':
                        line_items.extend(child.children)
                        break
            else:
                for child in element.children:
                    find_line_items(child)

        if isinstance(role, str):
            if role in self.roles:
                find_line_items(self.roles[role])
        else:
            find_line_items(role)

        return line_items

    def list_roles(self) -> List[str]:
        """ List all available roles in the presentation linkbase. """
        return list(self.roles.keys())

    def get_skipped_roles(self):
        return self.skipped_roles

    def get_structure(self, role_name: Optional[str] = None, detailed: bool = False) -> Optional[Tree]:
        """
        Get the presentation structure for a specific role.
        """
        if role_name:
            if role_name in self.roles:
                tree = Tree(f"[bold blue]{role_name}[/bold blue]")
                self._build_rich_tree(self.roles[role_name], tree, detailed)
                return tree
        else:
            main_tree = Tree("[bold green]XBRL Presentation Structure[/bold green]")
            for role, element in self.roles.items():
                role_tree = main_tree.add(f"[bold blue]{role}[/bold blue]")
                self._build_rich_tree(element, role_tree, detailed=detailed)
            return main_tree

    def __rich__(self):
        main_tree = Tree("[bold green]XBRL Presentation Structure[/bold green]")
        for role, element in self.roles.items():
            role_tree = main_tree.add(f"[bold blue]{role}[/bold blue]")
            self._build_rich_tree(element, role_tree, detailed=False)
        return main_tree

    def __repr__(self):
        return repr_rich(self)

    def print_structure(self, role: Optional[str] = None, detailed: bool = False):
        # Print the presentation structure using Rich library's Tree
        rprint('')
        if role:
            if role in self.roles:
                tree = self.get_structure(role, detailed)
                rprint(tree)
            else:
                print(f"Role '{role}' not found in the presentation linkbase.")
        else:
            rprint(self.__rich__())

    def _build_rich_tree(self, element: PresentationElement, tree: Tree, detailed: bool):
        # Recursively build the Rich Tree structure for visualization
        for child in sorted(element.children, key=lambda x: x.order):
            if detailed:
                # Detailed view: show full label, concept, and preferred label
                node_text = f"[green]{child.label}[/green] ([cyan]{child.href.split('#')[-1]}[/cyan])"
                if child.preferred_label:
                    node_text += f" [magenta]PL: {child.preferred_label.split('/')[-1]}[/magenta]"
            else:
                # Simplified view: show only namespace and first part of the name
                concept = child.href.split('#')[-1]
                namespace, name = concept.split('_', 1)
                # Just use the first part of the name .. some have a suffix like _xxx, etc.
                name = name.split('_')[0]
                node_text = Text.assemble((namespace, "bold grey70"), " ", (name, "bold deep_sky_blue1"))

            child_tree = tree.add(node_text)
            self._build_rich_tree(child, child_tree, detailed)


def get_axes_for_role(role: PresentationElement) -> List[PresentationElement]:
    """
    Find all axes (dimensions) for a given role.

     Args:
        role: Either a role name (str) or a PresentationElement

    Returns:
        List of axis names
    """
    axes = []

    def find_axes(element):
        if element.node_type == 'Table':
            for child in element.children:
                if child.node_type == 'Axis':
                    axes.append(child)
        else:
            for child in element.children:
                find_axes(child)

    find_axes(role)

    return axes


def get_members_for_axis(axis_element:PresentationElement,
                         ) -> List[str]:
    """
    List all members for a given axis in a specific role.

    Args:
        axis: The presentation element

    Returns:
        List of member names
    """
    members = []

    for domain in axis_element.children:
        if domain.node_type == 'Domain':
            for member in domain.children:
                if member.node_type == 'Member':
                    members.append(member)

    return members


def get_root_element(element: PresentationElement) -> PresentationElement:
    """Navigate up to find the root element"""
    current = element
    while current.parent:
        current = current.parent
    return current


class FinancialStatementMapper:
    STANDARD_STATEMENTS = {
        'BALANCE_SHEET': [
            'CONSOLIDATEDBALANCESHEETS',
            'CONSOLIDATEDBALANCESHEET',
            'COMPREHENSIVEBALANCESHEETS',
            'COMPREHENSIVEBALANCESHEET',
            'BALANCESHEET',
            'BALANCESHEETS',
            'STATEMENTOFFINANCIALPOSITION',
            'STATEMENTSOFFINANCIALPOSITION',
            'CONSOLIDATEDSTATEMENTOFFINANCIALPOSITION',
            'CONSOLIDATEDSTATEMENTSOFFINANCIALPOSITION'
        ],
        'INCOME_STATEMENT': [
            'CONSOLIDATEDSTATEMENTSOFOPERATIONS',
            'CONSOLIDATEDSTATEMENTOFOPERATIONS',
            'STATEMENTSOFOPERATIONS',
            'STATEMENTOFOPERATIONS',
            'INCOMESTATEMENT',
            'INCOMESTATEMENTS',
            'CONSOLIDATEDINCOMESTATEMENT',
            'CONSOLIDATEDINCOMESTATEMENTS',
            'STATEMENTSOFINCOME',
            'STATEMENTOFINCOME',
            'CONSOLIDATEDSTATEMENTSOFINCOME',
            'CONSOLIDATEDSTATEMENTOFINCOME',
            'CONSOLIDATEDSTATEMENTSOFINCOMELOSS',
            'CONSOLIDATEDSTATEMENTOFINCOMELOSS',
            'STATEMENTSOFEARNINGS',
            'STATEMENTOFEARNINGS',
            'CONSOLIDATEDSTATEMENTSOFEARNINGS',
            'CONSOLIDATEDSTATEMENTOFEARNINGS'
        ],
        'CASH_FLOW': [
            'CONSOLIDATEDSTATEMENTSOFCASHFLOWS',
            'CONSOLIDATEDSTATEMENTOFCASHFLOWS',
            'STATEMENTOFCASHFLOWS',
            'STATEMENTSOFCASHFLOWS',
            'CASHFLOWSTATEMENT',
            'CASHFLOWSTATEMENTS'
        ],
        'EQUITY': [
            'CONSOLIDATEDSTATEMENTSOFSHAREHOLDERSEQUITY',
            'CONSOLIDATEDSTATEMENTOFSHAREHOLDERSEQUITY',
            'CONSOLIDATEDSTATEMENTSOFSTOCKHOLDERSEQUITY',
            'CONSOLIDATEDSTATEMENTOFSTOCKHOLDERSEQUITY',
            'STATEMENTOFSHAREHOLDERSEQUITY',
            'STATEMENTOFSTOCKHOLDERSEQUITY',
            'STATEMENTSOFCHANGESINEQUITY',
            'STATEMENTOFCHANGESINEQUITY',
            'CONSOLIDATEDSTATEMENTSOFCHANGESINEQUITY',
            'CONSOLIDATEDSTATEMENTOFCHANGESINEQUITY',
            'STATEMENTOFEQUITY',
            'STATEMENTSOFEQUITY'
        ],
        'COMPREHENSIVE_INCOME': [
            'CONSOLIDATEDSTATEMENTSOFCOMPREHENSIVEINCOME',
            'CONSOLIDATEDSTATEMENTOFCOMPREHENSIVEINCOME',
            'STATEMENTOFCOMPREHENSIVEINCOME',
            'STATEMENTSOFCOMPREHENSIVEINCOME',
            'COMPREHENSIVEINCOMESTATEMENT',
            'COMPREHENSIVEINCOMESTATEMENTS'
        ],
        'COVER_PAGE': [
            'COVERPAGE',
            'COVER',
            'DOCUMENTANDENTITYINFORMATION',
            'ENTITYINFORMATION'
        ]
    }

    @classmethod
    def get_standard_name(cls, role_name: str) -> Optional[str]:
        # Extract the last part of the URI and remove any file extensions
        role_name = role_name.split('/')[-1].split('.')[0]

        # Normalize the role name: remove non-alphanumeric characters and convert to uppercase
        role_name_normalized = ''.join(char.upper() for char in role_name if char.isalnum())

        for standard_name, variations in cls.STANDARD_STATEMENTS.items():
            for variation in variations:
                if variation == role_name_normalized:
                    return standard_name

        return None
