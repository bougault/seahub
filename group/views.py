# encoding: utf-8
import os
import simplejson as json
from django.core.urlresolvers import reverse
from django.contrib import messages
from django.http import HttpResponse, HttpResponseRedirect, Http404, \
    HttpResponseBadRequest
from django.shortcuts import render_to_response, redirect
from django.template import RequestContext
from django.template.loader import render_to_string
from django.utils.http import urlquote

from auth.decorators import login_required
from seaserv import ccnet_rpc, ccnet_threaded_rpc, seafserv_threaded_rpc, \
    get_repo, get_group_repos, check_group_staff, get_commits, \
    get_personal_groups, get_group, get_group_members, create_org_repo, \
    get_org_group_repos
from pysearpc import SearpcError

from models import GroupMessage, MessageReply, MessageAttachment
from forms import MessageForm, MessageReplyForm, GroupRecommendForm
from signals import grpmsg_added, grpmsg_reply_added
from base.decorators import ctx_switch_required
from seahub.contacts.models import Contact
from seahub.contacts.signals import mail_sended
from seahub.notifications.models import UserNotification
from seahub.profile.models import Profile
from seahub.settings import SITE_ROOT
from seahub.shortcuts import get_first_object_or_none
from seahub.utils import render_error, render_permission_error, \
    validate_group_name, string2list, check_and_get_org_by_group
from seahub.views import is_registered_user
from seahub.forms import RepoCreateForm

@login_required
def group_list(request):
    error_msg = None
    if request.method == 'POST':
        """
        Add new group.
        """
        result = {}
        content_type = 'application/json; charset=utf-8'

        group_name = request.POST.get('group_name')
        if not validate_group_name(group_name):
            result['error'] = u'小组名称只能包含中英文字符，数字及下划线。'
            return HttpResponse(json.dumps(result), content_type=content_type)
        
        try:
            group_id = ccnet_threaded_rpc.create_group(group_name.encode('utf-8'),
                                   request.user.username)
        except SearpcError, e:
            result['error'] = e.msg
            return HttpResponse(json.dumps(result), content_type=content_type)
        return HttpResponse(json.dumps({'success': True}),
                            content_type=content_type)

    groups = get_personal_groups(request.user.username);
    
    return render_to_response("group/groups.html", {
            "groups": groups,
            }, context_instance=RequestContext(request))

@login_required
def group_remove(request, group_id):
    """
    Remove group from groupadmin page. Only system admin can perform this
    operation.
    """
    # Check whether user is system admin.
    if not request.user.is_staff:
        return render_permission_error(request, u'只有管理员有权删除小组')
        
    # Request header may missing HTTP_REFERER, we need to handle that case.
    next = request.META.get('HTTP_REFERER', None)
    if not next:
        next = SITE_ROOT

    try:
        group_id_int = int(group_id)
    except ValueError:
        return HttpResponseRedirect(next)

    try:
        ccnet_threaded_rpc.remove_group(group_id_int, request.user.username)
        seafserv_threaded_rpc.remove_repo_group(group_id_int, None)
    except SearpcError, e:
        return render_error(request, e.msg)

    return HttpResponseRedirect(next)

@login_required
def group_dismiss(request, group_id):
    """
    Dismiss a group, only group staff can perform this operation.
    """
    next = request.META.get('HTTP_REFERER', None)
    if not next:
        next = SITE_ROOT

    try:
        group_id_int = int(group_id)
    except ValueError:
        return HttpResponseRedirect(next)

    # Check whether user is group staff
    user = request.user.username
    if not ccnet_threaded_rpc.check_group_staff(group_id_int, user):
        return render_permission_error(request, u'只有小组管理员有权解散小组')

    try:
        ccnet_threaded_rpc.remove_group(group_id_int, user)
        seafserv_threaded_rpc.remove_repo_group(group_id_int, None)

        if request.user.org:
            org_id = request.user.org['org_id']
            url_prefix = request.user.org['url_prefix']
            ccnet_threaded_rpc.remove_org_group(org_id, group_id_int)
            return HttpResponseRedirect(reverse('org_groups',
                                                args=[url_prefix]))

    except SearpcError, e:
        return render_error(request, e.msg)
    
    return HttpResponseRedirect(reverse('group_list'))

@login_required
def group_quit(request, group_id):
    try:
        group_id_int = int(group_id)
    except ValueError:
        return render_error(request, u'group id 不是有效参数')
    
    try:
        ccnet_threaded_rpc.quit_group(group_id_int, request.user.username)
        seafserv_threaded_rpc.remove_repo_group(group_id_int,
                                                request.user.username)
    except SearpcError, e:
        return render_error(request, e.msg)
        
    return HttpResponseRedirect(reverse('group_list', args=[]))

def render_group_info(request, group_id, form):
    try:
        group_id_int = int(group_id)
    except ValueError:
        return HttpResponseRedirect(reverse('group_list', args=[]))

    # Check whether user belong to the group or admin
    joined = False
    groups = ccnet_threaded_rpc.get_groups(request.user.username)
    for group in groups:
        if group.id == group_id_int:
            joined = True
    if not joined and not request.user.is_staff:
        return render_error(request, u'未加入该小组')

    # if request.user.org and not request.user.org.is_staff:
    #     return render_error(request, u'未加入该小组')

    group = get_group(group_id)
    if not group:
        return HttpResponseRedirect(reverse('group_list', args=[]))

    if check_group_staff(group.id, request.user):
        is_staff = True
    else:
        is_staff = False
        
    members = get_group_members(group_id_int)
    managers = []
    common_members = []
    for member in members:
#        member.short_username = member.user_name.split('@')[0]
        if member.is_staff == 1:
            managers.append(member)
        else:
            common_members.append(member)

    org = request.user.org
    if org:
        repos = get_org_group_repos(org['org_id'], group_id_int,
                                    request.user.username)
    else:
        repos = get_group_repos(group_id_int, request.user.username)

    # remove user notifications
    UserNotification.objects.filter(to_user=request.user.username,
                                    msg_type='group_msg',
                                    detail=str(group_id)).delete()
    
    """group messages"""
    # Make sure page request is an int. If not, deliver first page.
    try:
        current_page = int(request.GET.get('page', '1'))
        per_page= int(request.GET.get('per_page', '15'))
    except ValueError:
        current_page = 1
        per_page = 15

    msgs_plus_one = GroupMessage.objects.filter(\
        group_id=group_id).order_by(\
        '-timestamp')[per_page*(current_page-1) : per_page*current_page+1]

    if len(msgs_plus_one) == per_page + 1:
        page_next = True
    else:
        page_next = False

    group_msgs = msgs_plus_one[:per_page]
    attachments = MessageAttachment.objects.filter(group_message__in=group_msgs)

    reply_to_list = []
    msg_replies = MessageReply.objects.filter(reply_to__in=group_msgs)
    for r in msg_replies:
        reply_to_list.append(r.reply_to_id)
    
    for msg in group_msgs:
        msg.reply_cnt = reply_to_list.count(msg.id)
            
        for att in attachments:
            if msg.id == att.group_message_id:
                # Attachment name is file name or directory name.
                # If is top directory, use repo name instead.
                path = att.path
                if path == '/':
                    repo = get_repo(att.repo_id)
                    if not repo:
                        # TODO: what should we do here, tell user the repo
                        # is no longer exists?
                        continue
                    att.name = repo.name
                else:
                    # cut out last '/'
                    if path[-1] == '/':
                        path = path[:-1]
                    att.name = os.path.basename(path)
                msg.attachment = att
                
    return render_to_response("group/group_info.html", {
            "managers": managers,
            "common_members": common_members,
            "members": members,
            "repos": repos,
            "group_id": group_id,
            "group" : group,
            "is_staff": is_staff,
            "is_join": joined,
            "group_msgs": group_msgs,
            "form": form,
            'current_page': current_page,
            'prev_page': current_page-1,
            'next_page': current_page+1,
            'per_page': per_page,
            'page_next': page_next,
            }, context_instance=RequestContext(request));

@login_required
def msg_reply(request, msg_id):
    """Show group message replies, and process message reply in ajax"""
    
    content_type = 'application/json; charset=utf-8'
    if request.is_ajax():
        ctx = {}
        if request.method == 'POST':
            form = MessageReplyForm(request.POST)

            # TODO: invalid form
            if form.is_valid():
                msg = form.cleaned_data['message']
                try:
                    group_msg = GroupMessage.objects.get(id=msg_id)
                except GroupMessage.DoesNotExist:
                    return HttpResponseBadRequest(content_type=content_type)
            
                msg_reply = MessageReply()
                msg_reply.reply_to = group_msg
                msg_reply.from_email = request.user.username
                msg_reply.message = msg
                msg_reply.save()

                # send signal if reply other's message
                if group_msg.from_email != request.user.username:
                    grpmsg_reply_added.send(sender=MessageReply,
                                            msg_id=msg_id,
                                            from_email=request.user.username)

                ctx['reply'] = msg_reply
                html = render_to_string("group/group_reply_new.html", ctx)

        else:
            try:
                msg = GroupMessage.objects.get(id=msg_id)
            except GroupMessage.DoesNotExist:
                raise HttpResponse(status=400)

            replies = MessageReply.objects.filter(reply_to=msg)
            ctx['replies'] = replies
            html = render_to_string("group/group_reply_list.html", ctx)

        serialized_data = json.dumps({"html": html})
        return HttpResponse(serialized_data, content_type=content_type)
    else:
        return HttpResponseBadRequest(content_type=content_type)

@login_required
def msg_reply_new(request):
    grpmsg_reply_list = []
    notes = UserNotification.objects.filter(to_user=request.user.username)
    for n in notes:
        if n.msg_type == 'grpmsg_reply':
            grpmsg_reply_list.append(n.detail)

    group_msgs = []
    for msg_id in grpmsg_reply_list:
        try:
            m = GroupMessage.objects.get(id=msg_id)
        except GroupMessage.DoesNotExist:
            continue
        else:
            # get group name
            group = get_group(m.group_id)
            if not group:
                continue
            m.group_name = group.group_name
            
            # get attachement
            attachment = get_first_object_or_none(m.messageattachment_set.all())
            if attachment:
                path = attachment.path
                if path == '/':
                    repo = get_repo(attachment.repo_id)
                    if not repo:
                        continue
                    attachment.name = repo.name
                else:
                    attachment.name = os.path.basename(path)
                m.attachment = attachment

            # get message replies
            reply_list = MessageReply.objects.filter(reply_to=m)

            m.reply_list = reply_list
            m.reply_cnt = reply_list.count()
            group_msgs.append(m)

    # remove new group msg reply notification
    UserNotification.objects.filter(to_user=request.user.username,
                                    msg_type='grpmsg_reply').delete()
    
    return render_to_response("group/new_msg_reply.html", {
            'group_msgs': group_msgs,
            }, context_instance=RequestContext(request))

@login_required
@ctx_switch_required
def group_info(request, group_id):
    if request.method == 'POST':
        form = MessageForm(request.POST)

        if form.is_valid():
            msg = form.cleaned_data['message']
            message = GroupMessage()
            message.group_id = group_id
            message.from_email = request.user.username
            message.message = msg
            message.save()

            # send signal
            grpmsg_added.send(sender=GroupMessage, group_id=group_id,
                              from_email=request.user.username)
            # Always return an HttpResponseRedirect after successfully dealing
            # with POST data.
            return HttpResponseRedirect(reverse('group_info', args=[group_id]))
    else:
        form = MessageForm()
        
        op = request.GET.get('op', '')
        if op == 'delete':
            return group_remove(request, group_id)
        elif op == 'dismiss':
            return group_dismiss(request, group_id)
        elif op == 'quit':
            return group_quit(request, group_id)

    return render_group_info(request, group_id, form)

@login_required
@ctx_switch_required
def group_members(request, group_id):
    try:
        group_id_int = int(group_id)
    except ValueError:
        return render_error(request, u'group id 不是有效参数')        

    if not check_group_staff(group_id_int, request.user):
        return render_permission_error(request, u'只有小组管理员有权管理小组')

    group = get_group(group_id)
    if not group:
        return HttpResponseRedirect(reverse('group_list', args=[]))
            
    if request.method == 'POST':
        """
        Add group members.
        """
        member_name_str = request.POST.get('user_name', '')

        member_list = string2list(member_name_str)

        if request.user.org:
            for member_name in member_list:
                # Add email to contacts
                mail_sended.send(sender=None, user=request.user.username,
                                  email=member_name)
                if not ccnet_threaded_rpc.org_user_exists(request.user.org['org_id'],
                                                 member_name):
                    err_msg = u'无法添加成员，当前企业不存在 %s 用户' % member_name
                    return render_error(request, err_msg)
                else:
                    try:
                        ccnet_threaded_rpc.group_add_member(group_id_int,
                                                   request.user.username,
                                                   member_name)
                    except SearpcError, e:
                        return render_error(request, e.msg)
        else:
            for member_name in member_list:
                # Add email to contacts
                mail_sended.send(sender=None, user=request.user.username,
                                  email=member_name)
                
                if not is_registered_user(member_name):
                    err_msg = u'无法添加成员，用户 %s 不存在' % member_name
                    return render_error(request, err_msg)
                else:
                    try:
                        ccnet_threaded_rpc.group_add_member(group_id_int,
                                                   request.user.username,
                                                   member_name)
                    except SearpcError, e:
                        return render_error(request, e.msg)
            
    members = ccnet_threaded_rpc.get_group_members(group_id_int)
    contacts = Contact.objects.filter(user_email=request.user.username)

    return render_to_response('group/group_manage.html', {
            'group' : group,
            'members': members,
            'contacts': contacts,
            }, context_instance=RequestContext(request))
    
@login_required
def group_member_operations(request, group_id, user_name):
    if request.GET.get('op','') == 'delete':
        return group_remove_member(request, group_id, user_name)
    else:
        return HttpResponseRedirect(reverse('group_members', args=[group_id]))

@login_required
def group_remove_member(request, group_id, user_name):
    try:
        group_id_int = int(group_id)
    except ValueError:
        return render_error(request, u'group id 不是有效参数')        
    
    if not check_group_staff(group_id_int, request.user):
        return render_permission_error(request, u'只有小组管理员有权删除成员')
    
    try:
        ccnet_threaded_rpc.group_remove_member(group_id_int,
                                               request.user.username,
                                               user_name)
        seafserv_threaded_rpc.remove_repo_group(group_id_int, user_name)
    except SearpcError, e:
        return render_error(request, e.msg)

    return HttpResponseRedirect(reverse('group_members', args=[group_id]))

def group_share_repo(request, repo_id, group_id, from_email):
    """
    Share a repo to a group.
    
    """
    # Check whether group exists
    group = get_group(group_id)
    if not group:
        return render_error(request, u'共享失败:小组不存在')
    
    # Check whether user belong to the group
    joined = False
    groups = ccnet_threaded_rpc.get_groups(request.user.username)
    for group in groups:
        if group.props.id == group_id:
            joined = True
    if not joined:
        return render_error(request, u'共享失败:未加入该小组')
    
    if seafserv_threaded_rpc.group_share_repo(repo_id, group_id, from_email, 'rw') != 0:
        return render_error(request, u'共享失败:内部错误')

def group_unshare_repo(request, repo_id, group_id, from_email):
    """
    Unshare a repo in group.
    
    """
    # Check whether group exists
    group = get_group(group_id)
    if not group:
        return render_error(request, u'取消共享失败:小组不存在')
    
    # Check whether user belong to the group
    joined = False
    groups = ccnet_threaded_rpc.get_groups(from_email)
    for group in groups:
        if group.props.id == group_id:
            joined = True
    if not joined:
        return render_error(request, u'取消共享失败:未加入该小组')

    # Check whether user is group staff or the one share the repo
    if not check_group_staff(group_id, from_email) and \
            seafserv_threaded_rpc.get_group_repo_owner(repo_id) != from_email:
        return render_permission_error(request, u'取消共享失败:只有小组管理员或共享目录发布者有权取消共享')
        
    if seafserv_threaded_rpc.group_unshare_repo(repo_id, group_id, from_email) != 0:
        return render_error(request, u'取消共享失败:内部错误')

@login_required
def group_recommend(request):
    """
    Recommend a file or directory to a group.
    """
    if request.method != 'POST':
        raise Http404

    next = request.META.get('HTTP_REFERER', None)
    if not next:
        next = SITE_ROOT
    
    form = GroupRecommendForm(request.POST)
    if form.is_valid():
        groups = form.cleaned_data['groups']
        repo_id = form.cleaned_data['repo_id']
        attach_type = form.cleaned_data['attach_type']
        path = form.cleaned_data['path']
        message = form.cleaned_data['message']

        group_list = string2list(groups)
        for e in group_list:
            group_name = e.split(' ')[0]
            try:
                group_creator = e.split(' ')[1]
            except IndexError:
                messages.add_message(request, messages.ERROR,
                                     u'推荐到 %s 失败，请检查小组名称。' % \
                                         group_name)
                continue
            
            # get all the groups the user joined
            groups = get_personal_groups(request.user.username)
            find = False
            for group in groups:
                # for every group that user joined, if group name and
                # group creator matchs, then has find the group
                if group.group_name == group_name and \
                        group_creator.find(group.creator_name) >= 0:
                    find = True
                    # save message to group
                    gm = GroupMessage(group_id=int(group.id),
                                      from_email=request.user.username,
                                      message=message)
                    gm.save()

                    # send signal
                    grpmsg_added.send(sender=GroupMessage, group_id=group.id,
                                      from_email=request.user.username)
                    
                    # save attachment
                    ma = MessageAttachment(group_message=gm, repo_id=repo_id,
                                           attach_type=attach_type, path=path,
                                           src='recommend')
                    ma.save()

                    group_url = reverse('group_info', args=[group.id])
                    msg = u'推荐到 <a href="%s" target="_blank">%s</a> 成功。' %\
                        (group_url, group_name)                    
                    messages.add_message(request, messages.INFO, msg)
                    break
            if not find:
                messages.add_message(request, messages.ERROR,
                                     u'推荐到 %s 失败，请检查是否参加了该小组。' % \
                                         group_name)
    else:
        # TODO: need more clear error message
        messages.add_message(request, messages.ERROR, '推荐失败')
    return HttpResponseRedirect(next)

@login_required
def create_group_repo(request, group_id):
    """Create a repo and share it to current group"""

    content_type = 'application/json; charset=utf-8'

    def json_error(err_msg):
        result = {'error': [err_msg]}
        return HttpResponseBadRequest(json.dumps(result),
                                      content_type=content_type)
    group_id = int(group_id)
    if not get_group(group_id):
        return json_error(u'创建失败:小组不存在')

    # Check whether user belong to the group
    groups = ccnet_threaded_rpc.get_groups(request.user.username)
    for group in groups:
        if group.props.id == group_id:
            break
    else:
        return json_error(u"创建失败:未加入该小组")

    form = RepoCreateForm(request.POST)
    if not form.is_valid():
        return json_error(form.errors)
    else:
        repo_name = form.cleaned_data['repo_name']
        repo_desc = form.cleaned_data['repo_desc']
        encrypted = form.cleaned_data['encryption']
        passwd = form.cleaned_data['passwd']
        user = request.user.username

        org, base_template = check_and_get_org_by_group(group_id, user)
        if org:
            # create group repo in org context
            try:
                repo_id = create_org_repo(repo_name, repo_desc, user, passwd,
                                          org.org_id)
            except:
                repo_id = None
            if not repo_id:
                return json_error(u"创建目录失败")

            try:
                status = seafserv_threaded_rpc.add_org_group_repo(repo_id,
                                                                  org.org_id,
                                                                  group_id,
                                                                  user, 'rw')
            except SearpcError, e:
                status = -1
                
            # if share failed, remove the newly created repo
            if status != 0:
                seafserv_threaded_rpc.remove_repo(repo_id)
                return json_error(u'创建目录失败:内部错误')
            else:
                result = {'success': True}
                return HttpResponse(json.dumps(result),
                                    content_type=content_type)
        else:
            # create group repo in user context
            try:
                repo_id = seafserv_threaded_rpc.create_repo(repo_name,
                                                            repo_desc,
                                                            user, passwd)
            except:
                repo_id = None
            if not repo_id:
                return json_error(u"创建目录失败")

            try:
                status = seafserv_threaded_rpc.group_share_repo(repo_id,
                                                                group_id,
                                                                user, 'rw')
            except SearpcError, e:
                status = -1
                
            # if share failed, remove the newly created repo
            if status != 0:
                seafserv_threaded_rpc.remove_repo(repo_id)
                return json_error(u'创建目录失败:内部错误')
            else:
                result = {'success': True}
                return HttpResponse(json.dumps(result),
                                    content_type=content_type)
