from datetime import date, timedelta

from .models import Backlog, Requirements, Sprint, SprintPlan, Team

FIBONACCI = (1, 2, 3, 5, 8, 13)
MOSCOW = {"must": 0, "should": 1, "could": 2, "wont": 3}


def plan(backlog: Backlog, team: Team, start: date, requirements: Requirements) -> tuple[Backlog, SprintPlan]:
    """Repair and schedule a copy. No clock, random values, model calls, or I/O."""
    backlog = backlog.model_copy(deep=True)
    tasks = {t.id: t for t in backlog.tasks}
    repairs, warnings = [], []
    for task in backlog.tasks:
        points = min(FIBONACCI, key=lambda x: (abs(x - task.points), x))
        if points != task.points:
            repairs.append(f"{task.id}: snapped points {task.points} to {points}")
            task.points = points
        dependencies = []
        for dep in task.dependencies:
            if dep not in tasks or dep == task.id or dep in dependencies:
                repairs.append(f"{task.id}: dropped invalid or duplicate dependency {dep}")
            else:
                dependencies.append(dep)
        task.dependencies = dependencies
        task.status, task.assignee, task.sprint = "todo", None, None

    visited, active = set(), set()

    def visit(task_id):
        active.add(task_id)
        task = tasks[task_id]
        for dep in list(task.dependencies):
            if dep in active:
                task.dependencies.remove(dep)
                repairs.append(f"{task_id}: broke cycle by dropping dependency {dep}")
            elif dep not in visited:
                visit(dep)
        active.remove(task_id)
        visited.add(task_id)

    for task_id in sorted(tasks):
        if task_id not in visited:
            visit(task_id)

    children = {task_id: [] for task_id in tasks}
    for task in tasks.values():
        for dep in task.dependencies:
            children[dep].append(task.id)
    tails, chains = {}, {}

    def tail(task_id):
        if task_id not in tails:
            successors = sorted(children[task_id])
            for successor in successors:
                tail(successor)
            best = min(successors, key=lambda x: (-tails[x], x)) if successors else None
            tails[task_id] = tasks[task_id].points + (tails[best] if best else 0)
            chains[task_id] = [task_id] + (chains[best] if best else [])
        return tails[task_id]

    for task_id in sorted(tasks):
        tail(task_id)
    head = min(tasks, key=lambda x: (-tails[x], x))
    capacities = {m.name: int(m.points_per_sprint * team.focus_factor) for m in team.members}
    members = sorted(team.members, key=lambda m: m.name)
    story_rank = {s.id: MOSCOW[s.priority] for s in requirements.stories}
    epic_rank = {e.id: e.priority for e in backlog.epics}
    for epic in backlog.epics:
        ranks = [story_rank[s] for t in backlog.tasks if t.epic == epic.id for s in t.story_ids if s in story_rank]
        if ranks:
            epic_rank[epic.id] = min(ranks)
    assigned, sprints = set(), []

    def order(task):
        return (task.priority, epic_rank[task.epic], -tails[task.id], task.id)

    while len(assigned) < len(tasks):
        number = len(sprints) + 1
        load = {name: 0 for name in capacities}
        selected = []
        for cross_role in ([False, True] if team.allow_cross_role else [False]):
            while True:
                progress = False
                ready = sorted(
                    (t for t in backlog.tasks if t.id not in assigned and set(t.dependencies) <= assigned),
                    key=order,
                )
                for task in ready:
                    eligible = [m for m in members if (m.role != task.role if cross_role else m.role == task.role)]
                    fitting = [m for m in eligible if load[m.name] + task.points <= capacities[m.name]]
                    if not fitting:
                        continue
                    member = min(fitting, key=lambda m: (load[m.name] / capacities[m.name], m.name))
                    task.assignee, task.sprint = member.name, number
                    selected.append(task.id)
                    assigned.add(task.id)
                    load[member.name] += task.points
                    if cross_role:
                        warnings.append(f"{task.id}: {member.name} assigned cross-role work ({task.role})")
                    progress = True
                if not progress:
                    break
        if not selected:
            ready = sorted((t for t in backlog.tasks if t.id not in assigned and set(t.dependencies) <= assigned), key=order)
            if not ready:
                raise ValueError("No schedulable tasks after dependency repair")
            task = ready[0]
            eligible = [m for m in members if m.role == task.role]
            if not eligible and team.allow_cross_role:
                eligible = members
                warnings.append(f"{task.id}: assigned cross-role work ({task.role})")
            if not eligible:
                raise ValueError(f"No team member can own role {task.role}; enable cross-role work or add a member")
            member = min(eligible, key=lambda m: (-capacities[m.name], m.name))
            task.assignee, task.sprint = member.name, number
            load[member.name] = task.points
            selected.append(task.id)
            assigned.add(task.id)
            warnings.append(f"{task.id}: exceeds member capacity; dedicated sprint, split it")
        sprint_start = start + timedelta(days=(number - 1) * team.sprint_length_days)
        sprints.append(Sprint(
            number=number, start=sprint_start, end=sprint_start + timedelta(days=team.sprint_length_days - 1),
            goal="; ".join(tasks[t].title for t in selected), tasks=selected,
            capacity=sum(capacities.values()), committed=sum(load.values()), load=load,
        ))
    return backlog, SprintPlan(
        sprints=sprints, critical_path=chains[head], critical_path_points=tails[head],
        tails=tails, projected_end=sprints[-1].end, warnings=warnings, repairs=repairs,
    )
