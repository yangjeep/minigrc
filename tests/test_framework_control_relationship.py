from app.models import ControlRequirementMapping, Framework, FrameworkRequirement, InternalControl


def test_control_maps_to_framework_requirement(app):
    session_factory = app.state.session_factory
    with session_factory() as session:
        framework = Framework(name="Test Framework", version="1.0", description="")
        session.add(framework)
        session.flush()

        requirement = FrameworkRequirement(
            framework_id=framework.id,
            reference_code="T.1",
            title="Test requirement",
            summary="",
        )
        session.add(requirement)
        session.flush()

        control = InternalControl(name="Test control", owner="owner@example.com")
        session.add(control)
        session.flush()

        session.add(ControlRequirementMapping(control_id=control.id, requirement_id=requirement.id))
        session.commit()

        session.refresh(control)
        session.refresh(requirement)

        assert len(control.mappings) == 1
        assert control.mappings[0].requirement.id == requirement.id
        assert len(requirement.mappings) == 1
        assert requirement.mappings[0].control.id == control.id
        assert requirement.framework.id == framework.id
