import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/client.dart';
import '../api/models.dart';
import '../state/jam.dart';
import '../state/session.dart';
import 'fx/motion.dart';
import 'fx/sparkle.dart';
import 'theme.dart';
import 'widgets.dart';

/// Start a Jam (pick who to invite), or manage the one you're in.
/// [tracks] and [index]: start from that list at that song, instead of from what's playing.
Future<void> showJamSheet(BuildContext context, WidgetRef ref, {List<Track>? tracks, int index = 0}) => showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useRootNavigator: true,
      builder: (_) => _JamSheet(tracks: tracks, index: index),
    );

class _JamSheet extends ConsumerStatefulWidget {
  const _JamSheet({this.tracks, this.index = 0});
  final List<Track>? tracks;
  final int index;
  @override
  ConsumerState<_JamSheet> createState() => _JamSheetState();
}

class _JamSheetState extends ConsumerState<_JamSheet> {
  final _picked = <int>{};
  bool _busy = false;
  late final Future<List<Person>> _people = ref.read(apiProvider)!.people();

  Future<void> _run(Future<void> Function() action, String done) async {
    setState(() => _busy = true);
    try {
      await action();
      if (mounted) {
        Navigator.pop(context);
        toast(context, done);
      }
    } on ApiException catch (e) {
      if (mounted) toast(context, e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final jam = ref.watch(jamProvider).jam;
    final ctl = ref.read(jamProvider.notifier);
    return SafeArea(
      child: Stack(children: [
        const Positioned.fill(child: AmbientSparkles(perSecond: 3)),
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 16),
          child: FutureBuilder<List<Person>>(
            future: _people,
            builder: (context, snap) {
              final people = snap.data ?? const <Person>[];
              final inJam = {...?jam?.members.map((m) => m.id), ...?jam?.invited.map((m) => m.id)};
              final choices = [for (final p in people) if (!inJam.contains(p.id)) p];
              return Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
                Row(children: [
                  const GradientMask(child: Icon(Icons.groups_rounded, size: 30)),
                  const SizedBox(width: 10),
                  Text(jam == null ? 'Start a Jam' : 'Your Jam', style: Theme.of(context).textTheme.headlineMedium),
                  if (jam != null) ...[const SizedBox(width: 10), const _LiveDot()],
                ]),
                const SizedBox(height: 6),
                Text(
                  jam == null
                      ? 'Listen together: everyone hears the same thing at the same moment, and anyone can play, pause, skip or add songs.'
                      : 'Hosted by ${jam.host.name}. Anyone in the Jam can control it.',
                  style: const TextStyle(color: muted, height: 1.35),
                ),
                if (jam != null) ...[
                  const SizedBox(height: 16),
                  for (final m in jam.members) _PersonRow(person: m, note: m.id == jam.host.id ? 'Host' : 'Listening'),
                  for (final m in jam.invited) _PersonRow(person: m, note: 'Invited', dim: true),
                ],
                const SizedBox(height: 14),
                if (snap.connectionState != ConnectionState.done)
                  const Padding(padding: EdgeInsets.all(12), child: Center(child: CircularProgressIndicator()))
                else if (choices.isNotEmpty) ...[
                  Text(jam == null ? 'Invite' : 'Invite more', style: const TextStyle(fontWeight: FontWeight.w800, color: muted, letterSpacing: 1)),
                  const SizedBox(height: 6),
                  for (final p in choices)
                    _PersonRow(
                      person: p,
                      trailing: Checkbox(
                        value: _picked.contains(p.id),
                        onChanged: (v) => setState(() => v == true ? _picked.add(p.id) : _picked.remove(p.id)),
                      ),
                      onTap: () => setState(() => _picked.contains(p.id) ? _picked.remove(p.id) : _picked.add(p.id)),
                    ),
                ] else if (jam == null)
                  const Text('Add family members on your server (People) to Jam with them.', style: TextStyle(color: muted)),
                const SizedBox(height: 16),
                if (jam == null)
                  GradientButton(
                    label: _picked.isEmpty ? 'Pick someone to invite' : 'Start the Jam',
                    onTap: _busy || _picked.isEmpty
                        ? null
                        : () => _run(() => ctl.start(_picked.toList(), tracks: widget.tracks, index: widget.index),
                            'Jam started: they\'ll get an invite'),
                  )
                else ...[
                  if (_picked.isNotEmpty)
                    GradientButton(label: 'Send invites', onTap: _busy ? null : () => _run(() => ctl.invite(_picked.toList()), 'Invites sent')),
                  const SizedBox(height: 8),
                  Center(
                    child: TextButton.icon(
                      icon: const Icon(Icons.logout_rounded, color: Color(0xFFFDA4AF)),
                      label: const Text('Leave the Jam', style: TextStyle(color: Color(0xFFFDA4AF))),
                      onPressed: _busy ? null : () => _run(ctl.leave, 'You left the Jam'),
                    ),
                  ),
                ],
              ]);
            },
          ),
        ),
      ]),
    );
  }
}

/// A round initial for someone in the family.
class PersonBadge extends StatelessWidget {
  const PersonBadge(this.name, {super.key, this.size = 40});
  final String name;
  final double size;

  @override
  Widget build(BuildContext context) {
    final hue = (name.codeUnits.fold<int>(0, (a, b) => a + b) * 47) % 360;
    final c1 = HSVColor.fromAHSV(1, hue.toDouble(), 0.55, 0.85).toColor();
    final c2 = HSVColor.fromAHSV(1, (hue + 50) % 360.0, 0.6, 0.7).toColor();
    return Container(
      width: size,
      height: size,
      alignment: Alignment.center,
      decoration: BoxDecoration(shape: BoxShape.circle, gradient: LinearGradient(colors: [c1, c2])),
      child: Text(name.isEmpty ? '?' : name.characters.first.toUpperCase(),
          style: TextStyle(fontWeight: FontWeight.w900, fontSize: size * 0.45)),
    );
  }
}

class _PersonRow extends StatelessWidget {
  const _PersonRow({required this.person, this.note, this.dim = false, this.trailing, this.onTap});
  final Person person;
  final String? note;
  final bool dim;
  final Widget? trailing;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) => InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(radiusM),
        child: Opacity(
          opacity: dim ? 0.6 : 1,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Row(children: [
              PersonBadge(person.name),
              const SizedBox(width: 12),
              Expanded(child: Text(person.name, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15.5))),
              if (note != null) Text(note!, style: const TextStyle(color: muted, fontSize: 12.5)),
              ?trailing,
            ]),
          ),
        ),
      );
}

class _LiveDot extends StatefulWidget {
  const _LiveDot();
  @override
  State<_LiveDot> createState() => _LiveDotState();
}

class _LiveDotState extends State<_LiveDot> with SingleTickerProviderStateMixin {
  late final _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 1200))..repeat(reverse: true);

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => FadeTransition(
        opacity: Tween(begin: 0.4, end: 1.0).animate(_c),
        child: Container(
          width: 10,
          height: 10,
          decoration: BoxDecoration(shape: BoxShape.circle, color: accent2, boxShadow: [BoxShadow(color: accent2.withValues(alpha: 0.8), blurRadius: 10)]),
        ),
      );
}

/// "JAM · Alex & Sam" on Now Playing; tap to manage.
class JamBanner extends ConsumerWidget {
  const JamBanner({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final jam = ref.watch(jamProvider).jam;
    return AnimatedSize(
      duration: const Duration(milliseconds: 300),
      child: jam == null
          ? const SizedBox(width: double.infinity)
          : Padding(
              padding: const EdgeInsets.fromLTRB(24, 10, 24, 0),
              child: Pressable(
                onTap: () => showJamSheet(context, ref),
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(20),
                    gradient: LinearGradient(colors: [accent.withValues(alpha: 0.35), accent2.withValues(alpha: 0.25)]),
                    border: Border.all(color: Colors.white.withValues(alpha: 0.15)),
                  ),
                  child: Row(children: [
                    const _LiveDot(),
                    const SizedBox(width: 10),
                    const Text('JAM', style: TextStyle(fontWeight: FontWeight.w900, letterSpacing: 2, fontSize: 12)),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(jam.members.map((m) => m.name).join(' & '), maxLines: 1, overflow: TextOverflow.ellipsis,
                          style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
                    ),
                    SizedBox(
                      width: 22.0 * jam.members.length.clamp(1, 4) + 8,
                      height: 26,
                      child: Stack(children: [
                        for (var i = 0; i < jam.members.length && i < 4; i++)
                          Positioned(left: i * 18.0, child: PersonBadge(jam.members[i].name, size: 26)),
                      ]),
                    ),
                  ]),
                ),
              ),
            ),
    );
  }
}

/// "Alex started a Jam": shown when an invite arrives.
Future<void> showJamInvite(BuildContext context, WidgetRef ref, JamInvite invite) => showModalBottomSheet<void>(
      context: context,
      useRootNavigator: true,
      builder: (sheet) {
        final item = invite.item;
        return SafeArea(
          child: Stack(children: [
            const Positioned.fill(child: AmbientSparkles(perSecond: 5)),
            Padding(
              padding: const EdgeInsets.fromLTRB(24, 0, 24, 20),
              child: Column(mainAxisSize: MainAxisSize.min, children: [
                PersonBadge(invite.from, size: 64),
                const SizedBox(height: 12),
                Text('${invite.from} started a Jam', style: Theme.of(context).textTheme.headlineMedium, textAlign: TextAlign.center),
                const SizedBox(height: 4),
                const Text('Listen together, right now', style: TextStyle(color: muted)),
                if (item != null) ...[
                  const SizedBox(height: 16),
                  Glass(
                    blur: false,
                    padding: const EdgeInsets.all(10),
                    child: Row(children: [
                      Cover(item.thumbUrl, size: 52),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                          Text(item.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
                          Text(item.artistLine, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: muted, fontSize: 13)),
                        ]),
                      ),
                      const EqualizerBars(playing: true),
                    ]),
                  ),
                ],
                const SizedBox(height: 20),
                Row(children: [
                  Expanded(
                    child: OutlinedButton(
                      onPressed: () {
                        Navigator.pop(sheet);
                        ref.read(jamProvider.notifier).decline(invite.id);
                      },
                      child: const Padding(padding: EdgeInsets.symmetric(vertical: 12), child: Text('Not now')),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: GradientButton(
                      label: 'Join',
                      onTap: () async {
                        Navigator.pop(sheet);
                        try {
                          await ref.read(jamProvider.notifier).join(invite.id);
                        } on ApiException catch (e) {
                          if (context.mounted) toast(context, e.message);
                        }
                      },
                    ),
                  ),
                ]),
              ]),
            ),
          ]),
        );
      },
    );

/// The little "JAM" tag on the mini player.
class JamTag extends StatelessWidget {
  const JamTag({super.key});

  @override
  Widget build(BuildContext context) => Container(
        margin: const EdgeInsets.only(right: 6),
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
        decoration: BoxDecoration(borderRadius: BorderRadius.circular(6), gradient: aurora),
        child: const Text('JAM', style: TextStyle(fontSize: 9.5, fontWeight: FontWeight.w900, letterSpacing: 1.2)),
      );
}
