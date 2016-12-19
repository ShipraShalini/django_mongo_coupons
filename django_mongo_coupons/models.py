import random
from bson import ObjectId

from datetime import datetime

from django.conf import settings
from django.db import IntegrityError
from django.db import models
from django.dispatch import Signal
from django.utils.encoding import python_2_unicode_compatible
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
# from django_mongoengine import Document as Document
# from django_mongoengine import fields as dj
from django_mongoengine.queryset import QuerySetManager
from mongoengine import Document, fields
from django_mongo_coupons.coupon_settings import User

from django_mongo_coupons.exceptions import CouponAlreadyUsed


from .coupon_settings import (
    COUPON_TYPES,
    CODE_LENGTH,
    CODE_CHARS,
    SEGMENTED_CODES,
    SEGMENT_LENGTH,
    SEGMENT_SEPARATOR,
)


# try:
#     user_model = settings.AUTH_USER_MODEL
# except AttributeError:
#     from django.contrib.auth.models import User as user_model
redeem_done = Signal(providing_args=["coupon"])


class CouponManager(QuerySetManager):
    def create_coupon(self, coupon_type, value, users=[], valid_until=None, prefix="", campaign=None, user_limit=None,
                      usage_limit=None):
        coupon = self.create(
            value=value,
            code=Coupon.generate_code(prefix),
            type=coupon_type,
            valid_until=valid_until,
            campaign=campaign,
        )
        if user_limit is not None:  # otherwise use default value of model
            coupon.user_limit = user_limit
        if usage_limit is not None:  # otherwise use default value of model
            coupon.usage_limit = usage_limit

        try:
            coupon.save()
        except IntegrityError:
            # Try again with other code
            coupon = Coupon.objects.create_coupon(type, value, users, valid_until, prefix, campaign)
        if not isinstance(users, list):
            users = [users]
        for user in users:
            if user:
                CouponUser(user=user, coupon=coupon).save()
        return coupon

    def create_coupons(self, quantity, type, value, valid_until=None, prefix="", campaign=None):
        coupons = []
        for i in range(quantity):
            coupons.append(self.create_coupon(type, value, None, valid_until, prefix, campaign))
        return coupons

    def used(self):
        return self.exclude(users__redeemed_at__exists=True)

    def unused(self):
        return self.filter(users__redeemed_at__exists=False)

    def expired(self):
        return self.filter(valid_until__lt=timezone.now())

    def valid(self):
        return self.filer(Q(users__redeemed_at__exists=False) & Q(valid_until__gt=datetime.utcnow()))


# @python_2_unicode_compatible
class Coupon(Document):
    value = fields.IntField(verbose_name="Value", help_text=_("Arbitrary coupon value"))
    code = fields.StringField(required=False, verbose_name="Code", unique=True, max_length=30, null=True)
    max_discount = fields.IntField(required=False, verbose_name="Maximum discount", null=True)
    type = fields.StringField(verbose_name="Type", max_length=20, choices=COUPON_TYPES)
    user_limit = fields.IntField(verbose_name="User limit", default=1, min_value=0)
    usage_limit = fields.IntField(verbose_name="Usage limit per user", default=1, min_value=0)
    created_at = fields.DateTimeField(verbose_name="Created at", default=datetime.utcnow())
    valid_until = fields.DateTimeField(verbose_name="Valid until", blank=True, null=True,
                                       help_text="Leave empty for coupons that never expire")
    campaign = fields.ReferenceField('Campaign', verbose_name="Campaign", blank=True, null=True,
                                     related_name='coupons', dbref=True)
    kwargs = fields.DictField(required=False, null=True)

    objects = CouponManager()

    meta = {
        'collection': "coupons",
        'indexes': ['code', 'valid_until']
    }

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = Coupon.generate_code()
        super(Coupon, self).save(*args, **kwargs)

    def expired(self):
        return self.valid_until is not None and self.valid_until < timezone.now()

    @property
    def is_redeemed(self):
        """ Returns true is a coupon is redeemed (completely for all users) otherwise returns false. """
        return self.users.filter(
            redeemed_at__exists=True
        ).count() >= self.user_limit and self.user_limit is not 0

    @property
    def redeemed_at(self):
        # try:
        return CouponUser.objects.get(coupon=self, redeemed_at__ne=[]).sort('-redeemed_at').first().redeemed_at[-1]
        #     # return self.users.filter(redeemed_at__exists=True).order_by('redeemed_at').last().redeemed_at
        # except CouponUser.through.DoesNotExist:
        #     return None

    @classmethod
    def generate_code(cls, prefix="", segmented=SEGMENTED_CODES):
        code = "".join(random.choice(CODE_CHARS) for i in range(CODE_LENGTH))
        if segmented:
            code = SEGMENT_SEPARATOR.join([code[i:i + SEGMENT_LENGTH] for i in range(0, len(code), SEGMENT_LENGTH)])
            return prefix + code
        else:
            return prefix + code

    def redeem(self, user=None):
        if user:
            user = User.objects.get(id=user)
        try:
            coupon_user = CouponUser.objects.get(coupon=self,
                                                 user=user)
            print "coupon_user", coupon_user
        except CouponUser.DoesNotExist:
            try:  # silently fix unbouned or nulled coupon users
                coupon_user = CouponUser.objects.get(user__exists=False)
                coupon_user.user = user
                print "here1"
            except CouponUser.DoesNotExist:
                print "here2"
                coupon_user = CouponUser(coupon=self, user=user)
        if self.usage_limit and coupon_user.redeemed_at and len(coupon_user.redeemed_at) >= self.usage_limit:
            raise CouponAlreadyUsed
        coupon_user.redeemed_at.append(timezone.now())
        coupon_user.save()
        redeem_done.send(sender=self.__class__, coupon=self)
        
    def apply_coupon(self, amount):
        '''amount: amount to be paid'''
        if self.type == 'percentage':
            discount = amount * self.value / 100
            try:
                if self.max_discount and discount > self.max_discount:
                    discount = self.max_discount
            except AttributeError:
                pass
        else:
            discount = self.value
        amount = amount - discount
        return amount if amount > 0 else 0


@python_2_unicode_compatible
class Campaign(Document):
    name = fields.StringField(max_length=255, unique=True)
    description = fields.StringField(null=True)
    kwargs = fields.DictField(required=False, null=True)

    meta = {
        'collection': "campaign"
    }

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class CouponUser(Document):
    coupon = fields.ReferenceField(Coupon, dbref=True)
    user = fields.ReferenceField(User, dbref=True, null=True) #
    # , unique_with=coupon)
    redeemed_at = fields.ListField(fields.DateTimeField(verbose_name="Redeemed at", null=True))
    kwargs = fields.DictField(required=False, null=True)

    meta = {
        'collection': "coupon_user",
        'indexes': ['coupon', 'user', 'redeemed_at' ]
    }

    def __str__(self):
        return str(self.user)