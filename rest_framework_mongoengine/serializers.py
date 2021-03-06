import copy
from collections import OrderedDict

from mongoengine import fields as me_fields
from mongoengine.errors import ValidationError as me_ValidationError
from rest_framework import fields as drf_fields
from rest_framework import serializers
from rest_framework.compat import unicode_to_repr
from rest_framework.utils.field_mapping import ClassLookupDict

from rest_framework_mongoengine.validators import (UniqueTogetherValidator,
                                                   UniqueValidator)

from rest_framework_mongoengine import fields as drfm_fields

from .repr import serializer_repr
from .utils import (COMPOUND_FIELD_TYPES, get_field_info, get_field_kwargs,
                    get_relation_kwargs, has_default, is_abstract_model)


def raise_errors_on_nested_writes(method_name, serializer, validated_data):
    # *** inherited from DRF 3, altered for EmbeddedDocumentSerializer to work automagically ***

    # Give explicit errors when users attempt to pass writable nested data.

    # If we don't do this explicitly they'd get a less helpful error when
    # calling `.save()` on the serializer.

    # We don't *automatically* support these sorts of nested writes because
    # there are too many ambiguities to define a default behavior.

    # Eg. Suppose we have a `UserSerializer` with a nested profile. How should
    # we handle the case of an update, where the `profile` relationship does
    # not exist? Any of the following might be valid:

    # * Raise an application error.
    # * Silently ignore the nested part of the update.
    # * Automatically create a profile instance.

    # Ensure we don't have a writable nested field. For example:
    #
    # class UserSerializer(ModelSerializer):
    #     ...
    #     profile = ProfileSerializer()
    assert not any(
        isinstance(field, serializers.BaseSerializer) and
        not isinstance(field, EmbeddedDocumentSerializer) and
        (key in validated_data)
        for key, field in serializer.fields.items()
    ), (
        'The `.{method_name}()` method does not support writable nested'
        'fields by default.\nWrite an explicit `.{method_name}()` method for '
        'serializer `{module}.{class_name}`, or set `read_only=True` on '
        'nested serializer fields.'.format(
            method_name=method_name,
            module=serializer.__class__.__module__,
            class_name=serializer.__class__.__name__
        )
    )

    # Ensure we don't have a writable dotted-source field. For example:
    #
    # class UserSerializer(ModelSerializer):
    #     ...
    #     address = serializer.CharField('profile.address')
    assert not any(
        '.' in field.source and (key in validated_data)
        and isinstance(validated_data[key], (list, dict))
        for key, field in serializer.fields.items()
    ), (
        'The `.{method_name}()` method does not support writable dotted-source '
        'fields by default.\nWrite an explicit `.{method_name}()` method for '
        'serializer `{module}.{class_name}`, or set `read_only=True` on '
        'dotted-source serializer fields.'.format(
            method_name=method_name,
            module=serializer.__class__.__module__,
            class_name=serializer.__class__.__name__
        )
    )


class DocumentSerializer(serializers.ModelSerializer):
    """ Serializer for Documents.

    Recognized primitve fields:

        * ``StringField``
        * ``URLField``
        * ``EmailField``
        * ``IntField``
        * ``LongField``
        * ``FloatField``
        * ``DecimalField``
        * ``BooleanField``
        * ``DateTimeField``
        * ``ComplexDateTimeField``
        * ``ObjectIdField``
        * ``SequenceField`` (assumes it has integer counter)
        * ``UUIDField``
        * ``GeoPointField``
        * ``GeoJsonBaseField`` (all those fields)

    Compound fields: ``ListField`` and ``DictField`` are mapped to corresponding DRF fields, with respect to nested field specification.

    The ``ReferenceField`` is handled like ``ForeignKey`` in DRF: there nested serializer autogenerated if serializer depth greater then 0, otherwise it's handled by it's own (results as ``str(id)``).

    For ``EmbeddedDocumentField`` also nested serializer autogenerated for non-zero depth, otherwise it is skipped. TODO: THIS IS PROBABLY WRONG AND SHOULD BE FIXED.

    Generic fields ``GenericReferenceField`` and ``GenericEmbeddedDocumentField`` are handled by their own with corresponding serializer fields.

    Not well supported or untested:

        ``FileField``
        ``ImageField``
        ``BinaryField``

    All other fields are mapped to ``DocumentField`` and probably will work wrong.
    """

    serializer_field_mapping = {
        me_fields.StringField: drf_fields.CharField,
        me_fields.URLField: drf_fields.URLField,
        me_fields.EmailField: drf_fields.EmailField,
        me_fields.IntField: drf_fields.IntegerField,
        me_fields.LongField: drf_fields.IntegerField,
        me_fields.FloatField: drf_fields.FloatField,
        me_fields.DecimalField: drf_fields.DecimalField,
        me_fields.BooleanField: drf_fields.BooleanField,
        me_fields.DateTimeField: drf_fields.DateTimeField,
        me_fields.ComplexDateTimeField: drf_fields.DateTimeField,
        me_fields.ObjectIdField: drfm_fields.ObjectIdField,
        me_fields.FileField: drfm_fields.FileField,
        me_fields.ImageField: drfm_fields.ImageField,
        me_fields.SequenceField: drf_fields.IntegerField,
        me_fields.UUIDField: drf_fields.UUIDField,
        me_fields.GeoPointField: drfm_fields.GeoPointField,
        me_fields.GeoJsonBaseField: drfm_fields.GeoJSONField,
        me_fields.BaseField: drfm_fields.DocumentField
    }

    # induct failure if they occasionally used somewhere
    serializer_related_field = None
    serializer_related_to_field = None
    serializer_url_field = None

    " class used to map ReferenceField "
    serializer_reference_field = drfm_fields.ReferenceField
    " class used to map GenericReferenceField "
    serializer_generic_reference_field = drfm_fields.GenericReferenceField
    " class used to map GenericEmbeddedDocumentField "
    serializer_generic_embedded_field = drfm_fields.GenericEmbeddedDocumentField

    def create(self, validated_data):
        raise_errors_on_nested_writes('create', self, validated_data)

        ModelClass = self.Meta.model
        try:
            instance = ModelClass(**validated_data)
            instance.save()
        except TypeError as exc:
            msg = (
                'Got a `TypeError` when calling `%s.objects.create()`. '
                'This may be because you have a writable field on the '
                'serializer class that is not a valid argument to '
                '`%s.objects.create()`. You may need to make the field '
                'read-only, or override the %s.create() method to handle '
                'this correctly.\nOriginal exception text was: %s.' %
                (
                    ModelClass.__name__,
                    ModelClass.__name__,
                    type(self).__name__,
                    exc
                )
            )
            raise TypeError(msg)
        except me_ValidationError as exc:
            msg = (
                'Got a `ValidationError` when calling `%s.objects.create()`. '
                'This may be because request data satisfies serializer validations '
                'but not Mongoengine`s. You may need to check consistency between '
                '%s and %s.\nIf that is not the case, please open a ticket '
                'regarding this issue on https://github.com/umutbozkurt/django-rest-framework-mongoengine/issues'
                '\nOriginal exception was: %s' %
                (
                    ModelClass.__name__,
                    ModelClass.__name__,
                    type(self).__name__,
                    exc
                )
            )
            raise me_ValidationError(msg)

        return instance

    def update(self, instance, validated_data):
        raise_errors_on_nested_writes('update', self, validated_data)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        return instance

    def get_model(self):
        return self.Meta.model

    def get_fields(self):
        assert hasattr(self, 'Meta'), (
            'Class {serializer_class} missing "Meta" attribute'.format(
                serializer_class=self.__class__.__name__
            )
        )
        assert hasattr(self.Meta, 'model'), (
            'Class {serializer_class} missing "Meta.model" attribute'.format(
                serializer_class=self.__class__.__name__
            )
        )
        depth = getattr(self.Meta, 'depth', 0)

        if depth is not None:
            assert depth >= 0, "'depth' may not be negative."
            assert depth <= 10, "'depth' may not be greater than 10."

        declared_fields = copy.deepcopy(self._declared_fields)
        model = self.get_model()

        if model is None:
            return {}

        if is_abstract_model(model):
            raise ValueError(
                'Cannot use ModelSerializer with Abstract Models.'
            )

        # Retrieve metadata about fields & relationships on the model class.
        self.field_info = get_field_info(model)
        field_names = self.get_field_names(declared_fields, self.field_info)
        # Determine any extra field arguments and hidden fields that
        # should be included
        extra_kwargs = self.get_extra_kwargs()
        extra_kwargs, hidden_fields = self.get_uniqueness_extra_kwargs(field_names, extra_kwargs)

        # Determine the fields that should be included on the serializer.
        fields = OrderedDict()

        for field_name in field_names:
            # If the field is explicitly declared on the class then use that.
            if field_name in declared_fields:
                fields[field_name] = declared_fields[field_name]
                continue

            # Determine the serializer field class and keyword arguments.
            field_class, field_kwargs = self.build_field(
                field_name, self.field_info, model, depth
            )

            extra_field_kwargs = extra_kwargs.get(field_name, {})
            field_kwargs = self.include_extra_kwargs(
                field_kwargs, extra_field_kwargs
            )

            # Create the serializer field.
            fields[field_name] = field_class(**field_kwargs)

        # Add in any hidden fields.
        fields.update(hidden_fields)

        return fields

    def get_field_names(self, declared_fields, model_info):
        field_names = super(DocumentSerializer, self).get_field_names(declared_fields, model_info)
        # filter out child fields
        return [fn for fn in field_names if '.child' not in fn]

    def get_default_field_names(self, declared_fields, model_info):
        return (
            [model_info.pk.name] +
            list(declared_fields.keys()) +
            list(model_info.fields.keys()) +
            list(model_info.references.keys()) +
            list(model_info.embedded.keys())
        )

    def build_field(self, field_name, info, model_class, nested_depth):
        if field_name in info.fields_and_pk:
            model_field = info.fields_and_pk[field_name]
            if isinstance(model_field, COMPOUND_FIELD_TYPES):
                child_name = field_name + '.child'
                if child_name in info.fields or child_name in info.embedded or child_name in info.references:
                    child_class, child_kwargs = self.build_field(child_name, info, model_class, nested_depth)
                    child_field = child_class(**child_kwargs)
                else:
                    child_field = None
                return self.build_compound_field(field_name, model_field, child_field)
            else:
                return self.build_standard_field(field_name, model_field)

        if field_name in info.references:
            relation_info = info.references[field_name]
            return self.build_reference_field(field_name, relation_info, nested_depth)

        if field_name in info.embedded:
            relation_info = info.embedded[field_name]
            return self.build_embedded_field(field_name, relation_info, nested_depth)

        if hasattr(model_class, field_name):
            return self.build_property_field(field_name, model_class)

        return self.build_unknown_field(field_name, model_class)

    def build_standard_field(self, field_name, model_field):
        field_mapping = ClassLookupDict(self.serializer_field_mapping)

        field_class = field_mapping[model_field]
        field_kwargs = get_field_kwargs(field_name, model_field)

        if 'choices' in field_kwargs:
            # Fields with choices get coerced into `ChoiceField`
            # instead of using their regular typed field.
            field_class = self.serializer_choice_field
            # Some model fields may introduce kwargs that would not be valid
            # for the choice field. We need to strip these out.
            # Eg. models.DecimalField(max_digits=3, decimal_places=1, choices=DECIMAL_CHOICES)
            valid_kwargs = set((
                'read_only', 'write_only',
                'required', 'default', 'initial', 'source',
                'label', 'help_text', 'style',
                'error_messages', 'validators', 'allow_null', 'allow_blank',
                'choices'
            ))
            for key in list(field_kwargs.keys()):
                if key not in valid_kwargs:
                    field_kwargs.pop(key)

        if not issubclass(field_class, drfm_fields.DocumentField):
            # `model_field` is only valid for the fallback case of
            # `ModelField`, which is used when no other typed field
            # matched to the model field.
            field_kwargs.pop('model_field', None)

        if not issubclass(field_class, drf_fields.CharField) and not issubclass(field_class, drf_fields.ChoiceField):
            # `allow_blank` is only valid for textual fields.
            field_kwargs.pop('allow_blank', None)

        if field_class is drf_fields.BooleanField and field_kwargs.get('allow_null', False):
            field_kwargs.pop('allow_null', None)
            field_kwargs.pop('default', None)
            field_class = drf_fields.NullBooleanField

        return field_class, field_kwargs

    def build_compound_field(self, field_name, model_field, child_field):
        if isinstance(model_field, me_fields.ListField):
            field_class = drf_fields.ListField
        elif isinstance(model_field, me_fields.DictField):
            field_class = drf_fields.DictField
        else:
            return self.build_unknown_field(field_name, model_field.owner_document)

        field_kwargs = get_field_kwargs(field_name, model_field)
        field_kwargs.pop('model_field', None)

        if child_field is not None:
            field_kwargs['child'] = child_field

        return field_class, field_kwargs

    def build_reference_field(self, field_name, relation_info, nested_depth):
        if relation_info.related_model and nested_depth:
            class NestedSerializer(DocumentSerializer):
                class Meta:
                    model = relation_info.related_model
                    depth = nested_depth - 1

            field_class = NestedSerializer
            field_kwargs = {'read_only': True}
        elif relation_info.related_model:
            field_class = self.serializer_reference_field
            field_kwargs = get_relation_kwargs(field_name, relation_info)
        else:
            field_class = self.serializer_generic_reference_field
            field_kwargs = get_field_kwargs(field_name, relation_info.model_field)
            field_kwargs.pop('model_field', None)

        return field_class, field_kwargs

    def build_embedded_field(self, field_name, relation_info, nested_depth):
        if not nested_depth:
            # just skip
            field_class = drf_fields.HiddenField
            field_kwargs = {'default': None}
        elif relation_info.related_model:
            class NestedSerializer(EmbeddedDocumentSerializer):
                class Meta:
                    model = relation_info.related_model
                    depth = nested_depth - 1

            field_class = NestedSerializer
            field_kwargs = get_field_kwargs(field_name, relation_info.model_field)
            field_kwargs.pop('model_field')
        else:
            field_class = self.serializer_generic_embedded_field
            field_kwargs = get_field_kwargs(field_name, relation_info.model_field)

        return field_class, field_kwargs

    def get_uniqueness_extra_kwargs(self, field_names, extra_kwargs):
        # extra_kwargs contains 'default', 'required', 'validators=[UniqValidator]'
        # hidden_fields contains fields involved in constraints, but missing in serializer fields
        model = self.Meta.model

        uniq_extra_kwargs = {}

        hidden_fields = {}

        field_names = set(field_names)
        unique_fields = set()
        unique_together_fields = set()

        # include `unique_with` from model indexes
        # so long as all the field names are included on the serializer.
        uniq_indexes = filter(lambda i: i.get('unique', False), model._meta.get('index_specs', []))
        for idx in uniq_indexes:
            field_set = set(map(lambda e: e[0], idx['fields']))
            if field_names.issuperset(field_set):
                if len(field_set) == 1:
                    unique_fields |= field_set
                else:
                    unique_together_fields |= field_set

        for field_name in unique_fields:
            uniq_extra_kwargs[field_name] = {
                'required': True,
                'validators': [UniqueValidator(queryset=model.objects)]
            }

        for field_name in unique_together_fields:
            fld = model._fields[field_name]
            if has_default(fld):
                uniq_extra_kwargs[field_name] = {'default': fld.default}
            else:
                uniq_extra_kwargs[field_name] = {'required': True}

        # Update `extra_kwargs` with any new options.
        for key, value in uniq_extra_kwargs.items():
            if key in extra_kwargs:
                if key == 'validators' and key in extra_kwargs:
                    extra_kwargs[key].append(value)
                extra_kwargs[key].update(value)
            else:
                extra_kwargs[key] = value

        return extra_kwargs, hidden_fields

    def get_unique_together_validators(self):
        model = self.Meta.model
        validators = []
        field_names = set(self.get_field_names(self._declared_fields, self.field_info))

        uniq_indexes = filter(lambda i: i.get('unique', False), model._meta.get('index_specs', []))
        for idx in uniq_indexes:
            if not idx.get('unique', False):
                continue
            field_set = tuple(map(lambda e: e[0], idx['fields']))
            if len(field_set) > 1 and field_names.issuperset(set(field_set)):
                validators.append(UniqueTogetherValidator(
                    queryset=model.objects,
                    fields=field_set
                ))
        return validators

    def get_unique_for_date_validators(self):
        # not supported in mongo
        return []

    def __repr__(self):
        return unicode_to_repr(serializer_repr(self, indent=1))


class EmbeddedDocumentSerializer(DocumentSerializer):
    """ Serializer for EmbeddedDocuments.

    Skips id field and uniqueness validation.
    """

    def get_default_field_names(self, declared_fields, model_info):
        # skip id field
        return (
            list(declared_fields.keys()) +
            list(model_info.fields.keys()) +
            list(model_info.references.keys()) +
            list(model_info.embedded.keys())
        )

    def get_unique_together_validators(self):
        # skip the valaidators
        return []

    def to_internal_value(self, data):
        # run nested validation and convert to document instance
        data = super(EmbeddedDocumentSerializer, self).to_internal_value(data)
        return self.Meta.model(**data)


class DynamicDocumentSerializer(DocumentSerializer):
    """ Serializer for DynamicDocuments.

    Maps all undefined fields to :class:`fields.DynamicField`.
    """
    def to_representation(self, instance):
        ret = super(DynamicDocumentSerializer, self).to_representation(instance)

        for field_name, field in self._map_dynamic_fields(instance).items():
            ret[field_name] = field.to_representation(field.get_attribute(instance))

        return ret

    def to_internal_value(self, data):
        ret = super(DocumentSerializer, self).to_internal_value(data)
        [drf_fields.set_value(ret, [k], data[k]) for k in data if k not in ret]
        return ret

    def _map_dynamic_fields(self, document):
        dynamic_fields = {}
        if document._dynamic:
            for name, field in document._dynamic_fields.items():
                dfield = drfm_fields.DynamicField(model_field=field, required=False)
                dfield.bind(name, self)
                dynamic_fields[name] = dfield
        return dynamic_fields
